"""Post-solve reports: technology sizing/cost table, bill of materials, energy mix plots,
and the energy-independence %.

Typical use (in a notebook, after solving):
    import run_microgrid, reporting
    inputs, sol, sdf, status = run_microgrid.solve()
    reporting.cost_table(inputs, sol)
    reporting.bom_table(inputs, sol)
    reporting.energy_summary(sdf)
    reporting.plot_energy_by_source(sdf)
    reporting.plot_dispatch(sdf, hours=168)
"""
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from excel_loader import INVERTER_SOLAR_RATIO
from run_microgrid import build_bom, _sizing_from_solution

CO2_GRID_G_PER_KWH = 380.0   # grid emission factor (gCO2 per kWh)


def _rating(solution_data, type_):
    return next((float(d["rating"]) for d in solution_data["data"] if d["type"] == type_), 0.0)


# --------------------------------------------------------------------------- #
#  Tables
# --------------------------------------------------------------------------- #
def cost_table(inputs, solution_data):
    """Technology sizes and costs that make up the full CapEx. The Solar and Battery lines use
    the EFFECTIVE per-unit cost the optimizer uses — panel + per_kW BOS, battery + per_kWh BOS
    (cabling, mounting, labor…). Panels still exclude the inverter (its own line). Fixed BOS is
    added as 'Engineering' + 'Fixed BOM' to complete the CapEx."""
    solar_kW = _rating(solution_data, "solar")
    batt_kWh = _rating(solution_data, "li")
    diesel_kW = _rating(solution_data, "diesel")
    rows = []
    if inputs.has_solar:
        inv_kW = max(INVERTER_SOLAR_RATIO * solar_kW, float(inputs.inverter_min_kW))
        solar_cost_per_kW = float(inputs.panel["cost_per_kW"]) + float(inputs.per_kw_solar)
        rows.append(["Solar panels", solar_kW, "kW", solar_cost_per_kW * solar_kW])
        rows.append(["Inverter", inv_kW, "kW", float(inputs.inverter["cost_per_kW"]) * inv_kW])
    if inputs.has_battery:
        batt_cost_per_kWh = float(inputs.battery["cost_per_kWh"]) + float(inputs.per_kwh_batt)
        rows.append(["Battery", batt_kWh, "kWh", batt_cost_per_kWh * batt_kWh])
    if "diesel" in inputs.tech_specs:
        rows.append(["Diesel generator", diesel_kW, "kW",
                     float(inputs.tech_specs["diesel"]["cost_per_kW"]) * diesel_kW])
        tank_kWh = _rating(solution_data, "diesel_fuel")
        if tank_kWh > 0:
            rows.append(["Fuel tank", tank_kWh, "kWh",
                         float(inputs.tank_specs["diesel_fuel"]["cost_per_kWh"]) * tank_kWh])
    df = pd.DataFrame(rows, columns=["Technology", "Size", "Unit", "Cost_$"])
    df["Size"] = df["Size"].round(1)
    df["Cost_$"] = df["Cost_$"].round(0)

    # Fixed BOS lumps complete the CapEx: an 'Engineering' line + an 'Fixed BOM' line (the rest).
    eng = other_fixed = 0.0
    for r in inputs.bos:
        if str(r["basis"]).strip() == "fixed":
            ext = float(r["unit_cost"]) * float(r["qty"])
            if "engineer" in str(r["item"]).lower():
                eng += ext
            else:
                other_fixed += ext
    df = pd.concat([df, pd.DataFrame([["Engineering", "", "", round(eng, 0)],
                                      ["Fixed BOM", "", "", round(other_fixed, 0)]],
                                     columns=df.columns)], ignore_index=True)
    return pd.concat([df, pd.DataFrame([["TOTAL CapEx", "", "", df["Cost_$"].sum()]],
                                       columns=df.columns)], ignore_index=True)


def bom_table(inputs, solution_data):
    """The itemised bill of materials (same content as bom.csv)."""
    solar_kW, batt_kWh = _sizing_from_solution(solution_data)
    diesel_kW = _rating(solution_data, "diesel")
    tank_kWh = _rating(solution_data, "diesel_fuel")
    rows, _ = build_bom(inputs, solar_kW, batt_kWh, diesel_kW, tank_kWh)
    return pd.DataFrame(rows)


def lcoe_table(solution_data):
    """LCoE and the implied annual / monthly payment."""

    costs = solution_data["meta"]["costs"]

    lcoe = float(costs["LCoE"])
    load = float(costs["LOAD"])
    annual_cost = lcoe * load

    rows = [
        ["LCoE", f"{lcoe:.4f}", "$/kWh"],
        ["Annual energy served", f"{load:,.0f}", "kWh"],
        ["Annual cost", f"{annual_cost:,.0f}", "$"],
        ["Monthly payment", f"{annual_cost / 12:,.0f}", "$/month"],
    ]
    grid_cost = costs.get("grid_cost")
    if grid_cost is not None:
        rows.append(["Grid (monthly)", f"{float(grid_cost) / 12:,.0f}", "$/month"])

    return pd.DataFrame(rows, columns=["Metric", "Value", "Unit"])


# --------------------------------------------------------------------------- #
#  Energy mix
# --------------------------------------------------------------------------- #
def _col(df, name):
    return df[name].to_numpy() if name in df.columns else np.zeros(len(df))


def energy_summary(solution_df):
    """Annual energy by source (kWh) + energy-independence %."""

    load = float(solution_df["LOAD"].sum())

    grid = float(_col(solution_df, "Power Purchased").sum())

    battery = float(_col(solution_df, "li to Load").sum())

    solar = float(
        (
            _col(solution_df, "Power Output - solar")
            - _col(solution_df, "solar to li")
        ).sum()
    )

    diesel = float(
        (
            _col(solution_df, "Power Output - diesel")
            - _col(solution_df, "diesel to li")
        ).sum()
    )

    return {
        "load_kWh": load,
        "grid_kWh": grid,
        "solar_kWh": solar,
        "battery_kWh": battery,
        "diesel_kWh": diesel,
        "energy_independence_pct": (1.0 - grid / load) * 100.0 if load else 0.0,
    }


def plot_energy_by_source(solution_df, ax=None):
    """Annual energy pie chart with Solar, Battery, Diesel, and Grid shown separately."""

    es = energy_summary(solution_df)

    items = [
        ("Solar", es["solar_kWh"], "#F4D03F"),
        ("Battery", es["battery_kWh"], "#2E86AB"),
        ("Diesel", es["diesel_kWh"], "#E1A100"),
        ("Grid", es["grid_kWh"], "#999999"),
    ]

    items = [(l, v, c) for l, v, c in items if v > 0]

    if ax is None:
        _, ax = plt.subplots(figsize=(5, 5))

    if items:
        labels, vals, colors = zip(*items)

        ax.pie(
            vals,
            labels=labels,
            colors=colors,
            autopct="%1.1f%%",
            startangle=90,
            wedgeprops={"edgecolor": "white"},
        )

    ax.axis("equal")
    ax.set_title(
        f"Energy by source  —  {es['energy_independence_pct']:.1f}% independent of grid"
    )

    return ax

def plot_dispatch(solution_df, start=0, hours=168, week=None, ax=None):
    """Stacked area of energy served to load over a window,
    grouped Solar / Battery / Diesel / Grid.
    Pass week=N (1-based, 168 h each) to show that week instead of `start`.
    """
    if week is not None:
        start = (week - 1) * hours

    s = solution_df.iloc[start:start + hours]
    t = np.arange(len(s))

    # Energy served directly from solar
    solar_served = (
        _col(s, "Power Output - solar")
        - _col(s, "solar to li")
    )

    # Energy served from battery discharge
    battery_served = _col(s, "li to Load")

    # Energy served from diesel
    diesel_served = (
        _col(s, "Power Output - diesel")
        - _col(s, "diesel to li")
    )

    # Energy served from grid
    grid_served = _col(s, "Power Purchased")

    if ax is None:
        _, ax = plt.subplots(figsize=(10, 4))

    ax.stackplot(
        t,
        solar_served,
        battery_served,
        diesel_served,
        grid_served,
        labels=["Solar", "Battery", "Diesel", "Grid"],
        colors=["#F4D03F", "#2E86AB", "#E1A100", "#999999"]
    )

    ax.plot(
        t,
        _col(s, "LOAD"),
        color="black",
        lw=1.2,
        label="Load"
    )

    ax.set_xlabel("hour")
    ax.set_ylabel("kW")
    ax.set_title("Energy served to load")
    ax.legend(loc="upper right", ncol=5, fontsize=8)
    ax.margins(x=0)

    return ax


def co2_summary(solution_df, g_per_kWh=CO2_GRID_G_PER_KWH):
    """Annual CO2 (tonnes): the 100%-grid baseline vs the actual grid emissions, and the offset."""
    load = float(solution_df["LOAD"].sum())
    grid = float(_col(solution_df, "Power Purchased").sum())
    to_t = g_per_kWh / 1e6                       # g/kWh -> tonnes per kWh
    baseline_t = load * to_t                      # if 100% of the load came from the grid
    grid_t = grid * to_t                          # actual grid emissions
    offset_t = baseline_t - grid_t                # CO2 avoided (solar + battery + diesel share)
    return {"baseline_t": baseline_t, "grid_t": grid_t, "offset_t": offset_t,
            "offset_pct": (offset_t / baseline_t * 100.0) if baseline_t else 0.0}


def plot_co2_pie(solution_df, ax=None):
    """Pie of the 100%-grid CO2 baseline split into offset (avoided) vs remaining grid emissions."""
    c = co2_summary(solution_df)
    if ax is None:
        _, ax = plt.subplots(figsize=(5, 5))
    ax.pie([c["offset_t"], c["grid_t"]],
           labels=[f"Offset\n{c['offset_t']:,.0f} t", f"Grid\n{c['grid_t']:,.0f} t"],
           colors=["#27AE60", "#999999"], autopct="%1.1f%%", startangle=90,
           wedgeprops={"edgecolor": "white"})
    ax.axis("equal")
    ax.set_title(f"CO₂ vs 100% grid  ({c['baseline_t']:,.0f} t/yr baseline)\n"
                 f"{c['offset_t']:,.0f} t/yr saved  ({c['offset_pct']:.0f}%)")
    return ax