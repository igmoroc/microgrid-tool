"""Run the microgrid optimization from the Excel/Sheet inputs; write solution.csv + bom.csv."""
import math
import os
import time
import urllib.request

import numpy as np
import pandas as pd

from excel_loader import load_inputs, build_optimizer_input, INVERTER_SOLAR_RATIO
from microgrid_highs import optimize_microgrid

T = 8760


def _read_series(path, n):
    """First numeric column of a csv/xlsx as a length-n float array."""
    frame = (pd.read_excel(path, engine="openpyxl")
             if path.lower().endswith((".xlsx", ".xls")) else pd.read_csv(path))
    nums = frame.select_dtypes("number")
    if nums.shape[1] == 0:
        raise ValueError(f"{path}: no numeric column found.")
    s = nums.iloc[:, 0].to_numpy(dtype=float)
    if len(s) != n:
        raise ValueError(f"{path}: expected {n} rows, got {len(s)}.")
    return s


def fetch_pvgis_tmy(lat, lon, dst=None, api="v5_2"):
    print("latlong", lat, lon)
    """Download (once, then cache) a PVGIS TMY csv for the coordinates; return its path."""
    # NOTE: delete the cached tmy_*.csv to force a fresh download for that site.
    if dst is None:
        dst = f"tmy_{lat:.5f}_{lon:.5f}.csv"
    if not os.path.exists(dst):
        url = f"https://re.jrc.ec.europa.eu/api/{api}/tmy?lat={lat}&lon={lon}&outputformat=csv"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = urllib.request.urlopen(req, timeout=90).read().decode("utf-8", errors="ignore")
        with open(dst, "w", encoding="utf-8") as f:
            f.write(data)
    return dst


def read_tmy_ghi(path, n, col="G(h)", stc=1000.0):
    """Per-unit GHI (0-~1.1) from a PVGIS TMY csv = the G(h) column / 1000 (STC reference)."""
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    hdr = next(i for i, l in enumerate(lines) if l.startswith("time(UTC)"))
    raw = pd.read_csv(path, skiprows=hdr, engine="python", on_bad_lines="skip")
    gh = pd.to_numeric(raw[col], errors="coerce").dropna().to_numpy()  # footer text -> NaN -> dropped
    if len(gh) < n:
        raise ValueError(f"{path}: only {len(gh)} numeric {col!r} rows, need {n}.")
    return gh[:n] / stc


def make_timeseries(inputs, n=T):
    """Return (load, ghi) arrays of length n."""
    # NOTE: load priority — a load.csv override, then the sheet's full hourly series (8760
    #       values used directly, no tiling), then the 24-h day profile tiled across all days.
    if os.path.exists("load.csv"):
        load = _read_series("load.csv", n)
    elif getattr(inputs, "load_hourly", None):
        full = np.asarray(inputs.load_hourly, dtype=float)            # full hourly series (e.g. 8760)
        load = full[:n] if len(full) >= n else np.tile(full, (n + len(full) - 1) // len(full))[:n]
    else:
        day = np.asarray(inputs.load_day_profile, dtype=float)        # 24 values
        load = np.tile(day, (n + len(day) - 1) // len(day))[:n]

    # NOTE: GHI is the PVGIS TMY at inputs.lat/lon (set 'lat'/'lon' in the project sheet).
    #       Synthetic bell is only a fallback if the download fails and there is no ghi.xlsx.
    try:
        ghi = read_tmy_ghi(fetch_pvgis_tmy(inputs.lat, inputs.lon), n)
    except Exception as e:
        print(f"PVGIS fetch failed ({e}); using fallback GHI.")
        if os.path.exists("ghi.xlsx"):
            ghi = _read_series("ghi.xlsx", n)
        else:
            hod = np.arange(n) % 24
            ghi = 0.85 * np.clip(np.sin(np.pi * (hod - 6) / 12), 0, None)
    return load, ghi


def build_df(inputs, n=T):
    load, ghi = make_timeseries(inputs, n)
    return pd.DataFrame({
        "total_energy_demand": load,
        "G(i)": ghi,                  # GHI 0-1; the solar derate is applied later, in f_l_t
        "tariff": inputs.grid_price,
    })


def _sizing_from_solution(solution_data):
    solar_kW = batt_kWh = 0.0
    for item in solution_data["data"]:
        if item["type"] == "solar":
            solar_kW = float(item["rating"])
        elif item["type"] == "li":
            batt_kWh = float(item["rating"])
    return solar_kW, batt_kWh


def build_bom(inputs, solar_kW, batt_kWh, diesel_kW=0.0, tank_kWh=0.0):
    """Itemized BOM rows + totals; component + per-unit + fixed must equal the model CapEx."""
    panel, battery, inverter = inputs.panel, inputs.battery, inputs.inverter
    rows = []

    def _r(v, nd):
        return round(v, nd) if isinstance(v, (int, float)) else v

    def add(section, item, qty, unit_cost, extended, note=""):
        rows.append({"section": section, "item": item, "qty": qty,
                     "unit_cost": _r(unit_cost, 4), "extended_cost": _r(extended, 2), "notes": note})

    inverter_kW = panel_count = batt_modules = inv_units = 0
    cost_panels = cost_inv = cost_batt = 0.0
    if panel is not None and solar_kW > 0:
        # Inverter sized as max(INVERTER_SOLAR_RATIO * solar, 1.25 * peak demand): >= the peak floor, else half the solar.
        inverter_kW = max(INVERTER_SOLAR_RATIO * solar_kW, float(inputs.inverter_min_kW))
        panel_count = math.ceil(solar_kW * 1000.0 / float(panel["rating_W"]))
        inv_units = math.ceil(inverter_kW / float(inverter["rating_kW"]))
        cost_panels = float(panel["cost_per_kW"]) * solar_kW
        cost_inv = float(inverter["cost_per_kW"]) * inverter_kW
        add("component", f"PV module: {panel['name']}", panel_count,
            cost_panels / panel_count if panel_count else 0.0, cost_panels, f"{panel['rating_W']} W each")
        add("component", f"Inverter: {inverter['name']}", inv_units,
            cost_inv / inv_units if inv_units else 0.0, cost_inv,
            f"{inverter['rating_kW']} kW each; sized {inverter_kW:.0f} kW = max({INVERTER_SOLAR_RATIO}*solar, load)")
    if battery is not None and batt_kWh > 0:
        batt_modules = math.ceil(batt_kWh / float(battery["module_kWh"]))
        cost_batt = float(battery["cost_per_kWh"]) * batt_kWh
        add("component", f"Battery module: {battery['name']}", batt_modules,
            cost_batt / batt_modules if batt_modules else 0.0, cost_batt, f"{battery['module_kWh']} kWh each")

    # Diesel generator + fuel tank (only when diesel is in the model and sized > 0).
    cost_diesel = cost_tank = 0.0
    if "diesel" in inputs.tech_specs and diesel_kW > 0:
        d_cpk = float(inputs.tech_specs["diesel"]["cost_per_kW"])
        cost_diesel = d_cpk * diesel_kW
        add("component", f"Diesel generator: {inputs.selection.get('diesel', '')}",
            round(diesel_kW, 1), d_cpk, cost_diesel, "$/kW × kW sized")
    if tank_kWh > 0:
        t_cpk = float(inputs.tank_specs["diesel_fuel"]["cost_per_kWh"])
        cost_tank = t_cpk * tank_kWh
        add("component", "Fuel tank (diesel)", round(tank_kWh, 1), t_cpk, cost_tank, "$/kWh × kWh storage")

    # NOTE: a line's `basis` decides where it goes — per_kW/per_kWh scale with sizing,
    #       fixed is flat, report_only is informational only. (Same rule used in excel_loader.)
    bos_perunit = bos_fixed = bos_report = 0.0
    for r in inputs.bos:
        basis, unit_cost, qty = r["basis"], float(r["unit_cost"]), float(r["qty"])
        if basis == "per_kW":
            ext = unit_cost * qty * solar_kW; bos_perunit += ext
            add("bos_per_kW", r["item"], qty, unit_cost, ext, r["notes"])
        elif basis == "per_kWh":
            ext = unit_cost * qty * batt_kWh; bos_perunit += ext
            add("bos_per_kWh", r["item"], qty, unit_cost, ext, r["notes"])
        elif basis == "fixed":
            ext = unit_cost * qty; bos_fixed += ext
            add("bos_fixed", r["item"], qty, unit_cost, ext, r["notes"])
        elif basis == "report_only":
            ext = unit_cost * qty; bos_report += ext
            add("report_only", r["item"], qty, unit_cost, ext, r["notes"])

    components = cost_panels + cost_inv + cost_batt + cost_diesel + cost_tank
    reconciled = components + bos_perunit + bos_fixed   # == model-implied CapEx
    total_capex = reconciled + bos_report
    totals = {"components": components, "bos_per_unit": bos_perunit, "bos_fixed": bos_fixed,
              "bos_report_only": bos_report, "reconciled_capex": reconciled, "total_capex": total_capex,
              "panel_count": panel_count, "batt_modules": batt_modules, "inv_units": inv_units,
              "inverter_kW": inverter_kW}
    for label, val in [("Components", components), ("BOS per-unit (in sizing)", bos_perunit),
                       ("BOS fixed", bos_fixed), ("BOS report-only", bos_report)]:
        add("subtotal", label, "", "", val)
    add("total", "TOTAL CapEx", "", "", total_capex)
    return rows, totals


def solve(source=None, n=T):
    """Load inputs, build the model and solve. Returns (inputs, solution_data, solution_df, status)."""
    inputs = load_inputs() if source is None else load_inputs(source)
    ns = build_optimizer_input(inputs, build_df(inputs, n), n)
    solution_data, solution_df, status = optimize_microgrid(ns)
    return inputs, solution_data, solution_df, status


def main():
    t0 = time.time()
    inputs, solution_data, solution_df, status = solve()   # default source = the Google Sheet
    elapsed = time.time() - t0

    solar_kW, batt_kWh = _sizing_from_solution(solution_data)
    diesel_kW = next((float(d["rating"]) for d in solution_data["data"] if d["type"] == "diesel"), 0.0)
    tank_kWh = next((float(d["rating"]) for d in solution_data["data"] if d["type"] == "diesel_fuel"), 0.0)
    costs = solution_data["meta"]["costs"]
    bom_rows, totals = build_bom(inputs, solar_kW, batt_kWh, diesel_kW, tank_kWh)

    solution_df.to_csv("solution.csv", index=False)
    pd.DataFrame(bom_rows).to_csv("bom.csv", index=False)

    # eff_solar carries only INVERTER_SOLAR_RATIO of the inverter; add the load-floor excess to reconcile.
    inv_cost_per_kW = float(inputs.inverter["cost_per_kW"]) if inputs.inverter else 0.0
    inverter_floor_excess = (totals["inverter_kW"] - INVERTER_SOLAR_RATIO * solar_kW) * inv_cost_per_kW
    diesel_capex = diesel_kW * float(inputs.tech_specs["diesel"]["cost_per_kW"]) if "diesel" in inputs.tech_specs else 0.0
    tank_capex = tank_kWh * float(inputs.tank_specs["diesel_fuel"]["cost_per_kWh"])
    model_capex = (solar_kW * inputs.eff_solar_cost_per_kW
                   + batt_kWh * inputs.eff_battery_cost_per_kWh + inputs.fixed_cost
                   + inverter_floor_excess + diesel_capex + tank_capex)
    drift = abs(model_capex - totals["reconciled_capex"])

    print(f"Status {status} in {elapsed:.1f}s")
    print(f"  solar {solar_kW:9.1f} kW   battery {batt_kWh:9.1f} kWh")
    if "diesel" in inputs.sets["technologies"]:
        print(f"  diesel {diesel_kW:9.1f} kW")
    print(f"  LCoE  ${costs['LCoE']:.4f}/kWh   CapEx ${totals['total_capex']:,.0f}")
    print(f"  BOM reconciliation drift ${drift:,.2f} -> {'OK' if drift < 1.0 else 'MISMATCH'}")
    print("  wrote solution.csv, bom.csv")


if __name__ == "__main__":
    main()
