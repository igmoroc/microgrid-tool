import pandas as pd
import numpy as np
import highspy
from math import ceil

# `input` is a plain SimpleNamespace; this is just a loose type-hint alias.
# (Do NOT import from a module named `app` here — it collides with the Streamlit app.py
#  and causes a circular import.)
HexalyModelInput = object


# --------------------------------------------------------------------------- #
#  Small helpers (HiGHS replacements for the old hexaly.utils functions)
# --------------------------------------------------------------------------- #
def _highs_value(h, x):
    """Value of a HiGHS variable / linear expression / plain constant, after solve."""
    if x is None:
        return 0.0
    if isinstance(x, (int, float, np.integer, np.floating)):
        return float(x)
    try:
        return float(h.val(x))
    except Exception:
        return 0.0


def _add(h, expr, name=None):
    """addConstr with optional name (falls back gracefully if names unsupported)."""
    try:
        return h.addConstr(expr, name=name) if name else h.addConstr(expr)
    except TypeError:
        return h.addConstr(expr)


# Hours per calendar month (non-leap). Used to bill monthly tariff service/demand charges.
_MONTH_HOURS = [744, 672, 744, 720, 744, 720, 744, 744, 720, 744, 720, 744]


def _month_groups(n):
    """List of (start, end) hour ranges, one per month, covering the first n hours."""
    groups, start = [], 0
    for mh in _MONTH_HOURS:
        if start >= n:
            break
        groups.append((start, min(start + mh, n)))
        start += mh
    if start < n:                       # horizon longer than a year -> remainder as one group
        groups.append((start, n))
    return groups or [(0, n)]


# =========================================================================== #
#  PASS 1 — continuous sizing + dispatch
# =========================================================================== #
def optimize_microgrid(input: HexalyModelInput):
    storage_specs = input.storage_specs
    project_specs = input.project_specs
    tank_specs = input.tank_specs
    grid_cut_hours = input.grid_cut_hours
    num_timesteps = input.num_timesteps
    tech_specs = input.tech_specs
    fuel_specs = input.fuel_specs
    grid_specs = input.grid_specs
    df = input.df
    fuel_burn_rates = input.fuel_burn_rates
    f_l_t = input.f_l_t
    sets = input.sets
    subsets = input.subsets

    # Unpack sets
    technologies = sets["technologies"]
    batteries = sets["batteries"]
    fuels = sets["fuels"]
    purchasing_fuels = subsets["purchasing_fuels"]
    fuel_technologies = subsets["fuel_technologies"]
    producing_technologies = subsets["producing_technologies"]
    fuel_consuming_technologies = subsets["fuel_consuming_technologies"]
    fuel_creating_technologies = subsets["fuel_creating_technologies"]
    diesel_technologies = subsets["diesel_technologies"]
    # Initialize optimizer
    h = highspy.Highs()
    h.silent()

    # Local value helpers (capture the solver handle so call-sites stay identical)
    def safe_value(x):
        return _highs_value(h, x)

    def safe_cost(x):
        return _highs_value(h, x)

    X_bkWh_b = {}
    X_t_sigma_s = {}
    MaxStorage_f = {}

    ######## SIZING VARIABLES ###########
    for b in batteries:
        if storage_specs[b]["size"] is not None:
            X_bkWh_b[b] = storage_specs[b]["size"]
        else:
            X_bkWh_b[b] = h.addVariable(lb=storage_specs[b]["min_capacity"],
                                        ub=storage_specs[b]["max_capacity"])

    for t in technologies:
        if tech_specs[t]["size"] is not None:
            X_t_sigma_s[t] = tech_specs[t]["size"]
        elif t == "fuel_cell" or t == "electrolyzer":
            X_t_sigma_s[t] = h.addVariable(lb=max(1, tech_specs[t]["min_output"]),
                                           ub=tech_specs[t]["max_output"])
        else:
            X_t_sigma_s[t] = h.addVariable(lb=tech_specs[t]["min_output"],
                                           ub=tech_specs[t]["max_output"])

    for f in fuels:
        if tank_specs[f]["size"] is not None:
            MaxStorage_f[f] = tank_specs[f]["size"]
        else:
            MaxStorage_f[f] = h.addVariable(lb=tank_specs[f]["min_capacity"],
                                            ub=tank_specs[f]["max_capacity"])

    # Operational variables (all bounds are constants taken from the specs)
    X_rp_th = {(t, hh): h.addVariable(lb=0, ub=tech_specs[t]["max_output"])
               for t in technologies for hh in range(num_timesteps)}
    X_f_th = {(t, f, hh): None for t in fuel_technologies for f in fuels for hh in range(num_timesteps)}
    purchase_hours = {hh for f in purchasing_fuels for hh in range(num_timesteps)
                      if hh % (tank_specs[f].get("frequency_hours")) == 0}

    X_f_p = {(f, hh): (h.addVariable(lb=0, ub=tank_specs[f]["max_capacity"]) if hh in purchase_hours else 0)
             for f in purchasing_fuels for hh in range(num_timesteps)}

    X_g_h = {hh: h.addVariable(lb=0, ub=grid_specs["grid_capacity"]) for hh in range(num_timesteps)}
    S_f_h = {(f, hh): h.addVariable(lb=0, ub=tank_specs[f]["max_capacity"])
             for f in fuels for hh in range(num_timesteps)}
    X_se_bh = {(b, hh): h.addVariable(lb=0, ub=storage_specs[b]["max_capacity"])
               for b in batteries for hh in range(num_timesteps)}
    # X_gts_h = {(b, hh): h.addVariable(lb=0, ub=min(grid_specs["grid_capacity"],
    #                                                storage_specs[b]["max_capacity"] * storage_specs[b]["c_value"]))
    #            for b in batteries for hh in range(num_timesteps)}

    X_pts_bth = {(b, t, hh): h.addVariable(lb=0, ub=storage_specs[b]["max_capacity"] * storage_specs[b]["c_value"])
                 for b in batteries for t in producing_technologies for hh in range(num_timesteps)}

    X_dfs_bth = {(b, hh): h.addVariable(lb=0, ub=storage_specs[b]["max_capacity"] * storage_specs[b]["c_value"])
                 for b in batteries for hh in range(num_timesteps)}

    # ---------------------- OBJECTIVE ----------------------
    tech_install_cost = h.qsum([
        tech_specs[t]["cost_per_kW"] / min(tech_specs[t]["lifetime"], project_specs["lifetime"])
        * (X_t_sigma_s[t] - tech_specs[t]["existing_rating"])
        + tech_specs[t]["maintenance_per_kW"] * (X_t_sigma_s[t] - tech_specs[t]["existing_rating"])
        for t in technologies
    ])

    storage_cost = h.qsum([
        storage_specs[b]["cost_per_kWh"] / min(storage_specs[b]["lifetime"], project_specs["lifetime"])
        * (X_bkWh_b[b] - storage_specs[b]["existing_rating"])
        + storage_specs[b]["maintenance_per_kWh"] * (X_bkWh_b[b] - storage_specs[b]["existing_rating"])
        for b in batteries
    ])

    fc = project_specs["fixed_cost"]
    fixed_cost = float(sum(fc)) if hasattr(fc, "__iter__") else float(fc)
    # Annualize the fixed (BOS) lump over the project lifetime so it is consistent with the
    # capex terms above (which are also divided by lifetime). `fixed_cost` stays the full lump
    # for the BOM / CapEx reporting; only the LCoE objective uses the annualized figure.
    annualized_fixed_cost = fixed_cost / project_specs["lifetime"]

    fuel_storage_cost = h.qsum([
        tank_specs[f]["cost_per_kWh"] / min(tank_specs[f]["lifetime"], project_specs["lifetime"])
        * (MaxStorage_f[f] - tank_specs[f]["existing_rating"])
        + tank_specs[f]["maintenance_per_kW"] * (MaxStorage_f[f] - tank_specs[f]["existing_rating"])
        for f in fuels
    ])

    fuel_purchase_costs = h.qsum([
        fuel_specs[f]["$/kWh"] * X_f_p[(f, hh)]
        for f in purchasing_fuels for hh in range(num_timesteps)
    ])

    initial_fuel_cost = h.qsum([fuel_specs[f]["$/kWh"] * MaxStorage_f[f] for f in purchasing_fuels])

    timestep_hours = 1.0


    tariff = getattr(input, "tariff_spec", None)
    service_cost = 0.0
    if tariff is None:
        grid_cost = (h.qsum([df["tariff"].iloc[hh] * X_g_h[hh] for hh in range(num_timesteps)])
                     if "tariff" in df.columns else 0.0)
    else:
        # Monthly tariff: tiered energy on grid import + a demand charge on the monthly peak grid
        # draw + a flat service charge. All rates are already converted to USD in the loader.
        energy_terms, demand_terms = [], []
        for (a, b) in _month_groups(num_timesteps):
            grid_month = h.qsum([X_g_h[hh] for hh in range(a, b)])
            if tariff["block_kWh"] > 0:                  # rising-block (e.g. D1): convex -> LP-friendly
                e_high = h.addVariable(lb=0, ub=grid_specs["grid_capacity"] * (b - a))
                _add(h, e_high >= grid_month - tariff["block_kWh"], f"Tariff block {a}")
                energy_terms.append(tariff["block_rate"] * grid_month
                                    + (tariff["energy_rate"] - tariff["block_rate"]) * e_high)
            else:
                energy_terms.append(tariff["energy_rate"] * grid_month)
            if tariff["demand_per_kW_month"] > 0:        # demand charge on this month's peak import
                peak_m = h.addVariable(lb=0, ub=grid_specs["grid_capacity"])
                for hh in range(a, b):
                    _add(h, peak_m >= X_g_h[hh], f"Grid peak {hh}")
                demand_terms.append(tariff["demand_per_kW_month"] * peak_m)
            service_cost += tariff["service_per_month"]
        grid_cost = h.qsum(energy_terms + demand_terms)

    # Discourage simultaneous charge & discharge in the same hour (LP-friendly, no binaries):
    # a tiny penalty on total battery throughput (charge + discharge). Charging from solar
    # AND discharging to load in the same hour is never beneficial (solar can serve load
    # directly, and the round trip loses efficiency), so any positive penalty makes the
    # optimum avoid it, while being far too small to affect sizing or LCoE.
    charge_discharge_penalty = 1e-4  # $ per kWh of throughput
    battery_throughput = h.qsum(
        [X_pts_bth[(b, t, hh)] for b in batteries for t in producing_technologies for hh in range(num_timesteps)]
        + [X_dfs_bth[(b, hh)] for b in batteries for hh in range(num_timesteps)]
    )

    total_cost = (tech_install_cost + storage_cost + fuel_storage_cost + grid_cost + service_cost
                  + fuel_purchase_costs + initial_fuel_cost
                  + annualized_fixed_cost + charge_discharge_penalty * battery_throughput)
    load_sum = float(df["total_energy_demand"].sum())
    LCoE = total_cost * (1.0 / load_sum)            # denominator is a constant
    h.minimize(LCoE)

    # ---------------------- CONSTRAINTS ----------------------
    X_b_0_se = {b: X_bkWh_b[b] for b in batteries}

    # Fuel technology constraints  ( m.min(...) -> two linear constraints )
    for t in fuel_technologies:
        for f in fuels:
            for hh in range(num_timesteps):
                X_f_th[(t, f, hh)] = fuel_burn_rates[t, f] * X_rp_th[(t, hh)]
                _add(h, X_f_th[(t, f, hh)] >= 0,
                     f"Min Generation Fuel Technologies {t}_{f}_{hh}")
                _add(h, X_f_th[(t, f, hh)] <= tech_specs[t]["max_output"] / tech_specs[t]["efficiency"],
                     f"Max Generation Fuel Technologies cap {t}_{f}_{hh}")
                _add(h, X_f_th[(t, f, hh)] <= MaxStorage_f[f],
                     f"Max Generation Fuel Technologies tank {t}_{f}_{hh}")

    # Fuel storage dynamics
    for f in fuels:
        for hh in range(num_timesteps):
            if hh == 0:
                _add(h, S_f_h[(f, hh)] == MaxStorage_f[f] * 0.5, f"Fuel Storage Dynamics {f}_{hh}")
            else:
                if f in purchasing_fuels:
                    _add(h, S_f_h[(f, hh)] == S_f_h[(f, hh - 1)]
                         - h.qsum([X_f_th[(t, f, hh - 1)] for t in fuel_technologies]) + X_f_p[(f, hh)],
                         f"Fuel Storage Dynamics {f}_{hh}")
                else:
                    _add(h, S_f_h[(f, hh)] == S_f_h[(f, hh - 1)]
                         - h.qsum([X_f_th[(t, f, hh - 1)] for t in fuel_technologies]),
                         f"Fuel Storage Dynamics {f}_{hh}")

    # Final state constraints
    for f in fuels:
        hh = num_timesteps - 1
        _add(h, S_f_h[(f, hh)] >= MaxStorage_f[f] * 0.5, f"Force half full at end {f}")
        _add(h, S_f_h[(f, hh)] == S_f_h[(f, hh - 1)]
             - h.qsum([X_f_th[(t, f, hh - 1)] for t in fuel_technologies]),
             f"Fuel Storage Dynamic reinforced {f}_{hh}")

    # Storage capacity constraints
    for f in fuels:
        for hh in range(num_timesteps):
            _add(h, S_f_h[(f, hh)] <= MaxStorage_f[f], f"Maximum Storage limit {f}_{hh}")
            _add(h, S_f_h[(f, hh)] >= MaxStorage_f[f] * tank_specs[f]["dod"], f"Minimum Storage limit {f}_{hh}")

    # Battery storage dynamics
    for hh in range(num_timesteps):
        for b in batteries:
            charge = (h.qsum([X_pts_bth[(b, t, hh)] for t in producing_technologies]))
            if hh == 0:
                _add(h, X_se_bh[(b, hh)] == X_b_0_se[b]
                     + storage_specs[b]["efficiency"] * charge - X_dfs_bth[(b, hh)],
                     f"Battery Storage Dynamics {hh}_{b}")
            else:
                _add(h, X_se_bh[(b, hh)] == X_se_bh[(b, hh - 1)]
                     + storage_specs[b]["efficiency"] * charge - X_dfs_bth[(b, hh)],
                     f"Battery Storage Dynamics {hh}_{b}")

    # Battery capacity constraints. The lower SoC floor can vary by season (battery_dod_jan/jul).
    soc_floor_hour = getattr(input, "soc_floor_hour", None)
    for hh in range(num_timesteps):
        for b in batteries:
            floor = soc_floor_hour[hh] if soc_floor_hour else storage_specs[b]["dod"]
            _add(h, X_se_bh[(b, hh)] <= X_bkWh_b[b], f"Battery Upper Limit {hh}_{b}")
            _add(h, X_se_bh[(b, hh)] >= floor * X_bkWh_b[b], f"Battery Lower Limit {hh}_{b}")

    # Charging limits
    for b in batteries:
        for t in producing_technologies:
            for hh in range(num_timesteps):
                _add(h, X_pts_bth[(b, t, hh)] <= f_l_t[t, hh] * X_rp_th[(t, hh)], f"Charging Limits {b}_{t}_{hh}")

    # Battery to load constraints
    for hh in range(num_timesteps):
        for b in batteries:
            _add(h, X_dfs_bth[(b, hh)] <= df["total_energy_demand"].iloc[hh]
                 + h.qsum([X_rp_th[(t, hh)] for t in fuel_creating_technologies]),
                 f"Battery to Load {hh}_{b}")

    # Battery power capacity constraints
    for hh in range(num_timesteps):
        for b in batteries:
            _add(h, X_bkWh_b[b] * storage_specs[b]["c_value"]
                 >= h.qsum([X_pts_bth[(b, t, hh)] for t in producing_technologies])
                 + X_dfs_bth[(b, hh)],
                 f"Battery Power Capacity Constraints {hh}_{b}")

    # Technology power output constraints
    for t in technologies:
        for hh in range(num_timesteps):
            _add(h, X_rp_th[(t, hh)] <= X_t_sigma_s[t], f"Technology power output constraints {t}_{hh}")

    # Grid constraints
    for hh in range(num_timesteps):
        _add(h, X_g_h[hh] <= grid_specs["grid_capacity"],
             f"Grid Constraint {hh}")

    # Grid outage: force grid import to 0 during the specified hours (load met by solar/battery/diesel).
    for hh in grid_cut_hours:
        if 0 <= hh < num_timesteps:
            _add(h, X_g_h[hh] == 0, f"Grid cut {hh}")

    # Annual grid-energy share limit: total energy drawn from the grid over the year
    max_grid_fraction = project_specs.get("max_grid_fraction", 0.9)
    annual_grid_energy = (
        h.qsum([X_g_h[hh] for hh in range(num_timesteps)])
    )
    _add(h, annual_grid_energy <= max_grid_fraction * load_sum, "Annual Grid Energy Share Limit")

    # Resiliency constraints
    # if project_specs["resiliency_days"] > 0:
    #     _add(h, h.qsum([X_bkWh_b[b] for b in batteries]) + h.qsum([MaxStorage_f[f] for f in fuels])
    #          >= project_specs["resiliency_days"] * project_specs["day_hours"] * df["total_energy_demand"].mean(),
    #          "Resiliency Constraints")
    #     for hh in grid_cut_hours:
    #         _add(h, X_g_h[hh] == 0, f"Set Grid to 0 {hh}")

    # Energy balance constraints
    for hh in range(num_timesteps):
        total_tech_output = h.qsum([f_l_t[t, hh] * X_rp_th[(t, hh)] for t in technologies])
        total_discharged = h.qsum([X_dfs_bth[(b, hh)] for b in batteries])
        total_charged = (h.qsum([X_pts_bth[(b, t, hh)] for b in batteries for t in producing_technologies])
                       )
        grid_purchases = X_g_h[hh]
        _add(h, total_tech_output + total_discharged + grid_purchases
             == df["total_energy_demand"].iloc[hh] + total_charged, f"Energy balance_{hh}")

    if project_specs["offgrid_base"]:
        for t in diesel_technologies:
            _add(h, h.qsum([X_rp_th[(t, hh)] for hh in range(num_timesteps)])
                 <= project_specs["max_diesel"] * h.qsum([df["total_energy_demand"].iloc[hh]
                                                          for hh in range(num_timesteps)]),
                 f"Diesel limit {t}")

    # ---------------------- SOLVE ----------------------
    # Optionally dump the fully-built model to a human-readable text file before solving.
    # Set input.write_model_path = "model.lp" (LP = readable, with the constraint names
    # used above) or "model.mps". NOTE: with num_timesteps=8760 the file is huge; use a
    # short horizon (e.g. num_timesteps=24) when you want to actually read the formulation.
    model_path = getattr(input, "write_model_path", None)
    if model_path:
        h.writeModel(model_path)
        print(f"Model written to {model_path}")

    h.setOptionValue("time_limit", 200.0)   # max runtime, like set_time_limit(200)
    h.setOptionValue("mip_rel_gap", 0.001)   # 0.1% gap, like the old gap callback
    h.run()

    status = h.getModelStatus()
    ms = highspy.HighsModelStatus
    if status == ms.kInfeasible:
        print("No feasible solution exists for the current model formulation. "
              "Formulation contradicts itself")
    elif status == ms.kTimeLimit:
        print("Solver hit the time limit. A solution may be available but optimality is not proven. "
              "You may give the solver more time for a better result.")
    elif status == ms.kUnbounded:
        print("Problem is unbounded — check costs/bounds.")
    elif status == ms.kOptimal:
        print("Optimal solution found! All constraints satisfied and objective gap is within tolerance.")
    else:
        print(f"Unexpected status: {h.modelStatusToString(status)}. Please check solver logs.")

    ####### POST PROCESSING #####
    solution_data = {"data": [], "meta": {}}

    # --- TECHNOLOGIES ---
    for tech in technologies:
        tech_spec = tech_specs.get("technologies", tech_specs).get(tech, {})
        rating = safe_value(X_t_sigma_s.get(tech))
        tco = (tech_spec.get("cost_per_kW", 0)
               * max(0, (safe_value(X_t_sigma_s.get(tech)) - tech_spec["existing_rating"]))
               * project_specs["lifetime"] / tech_spec.get("lifetime", 1))
        solution_data["data"].append({
            "category": "technology", "type": tech, "TCO_1st_installation": tco, "rating": rating,
            "capex": (rating - tech_spec.get("existing_rating", 0)) * tech_spec.get("cost_per_kW", 0),
            "lifetime": tech_spec.get("lifetime"),
        })

    # --- BATTERIES ---
    for batt in batteries:
        batt_spec = storage_specs.get(batt, {})
        rating = safe_value(X_bkWh_b.get(batt))
        tco = (batt_spec.get("cost_per_kWh", 0)
               * max(0, (safe_value(X_bkWh_b.get(batt)) - batt_spec["existing_rating"]))
               * project_specs["lifetime"] / batt_spec.get("lifetime", 1))
        solution_data["data"].append({
            "category": "storage", "type": batt, "lifetime": batt_spec.get("lifetime"),
            "TCO_1st_installation": tco, "rating": rating,
            "capex": (rating - batt_spec.get("existing_rating", 0)) * batt_spec.get("cost_per_kWh", 0),
        })

    # --- FUEL TANKS ---
    for fuel in fuels:
        tank_spec = tank_specs.get(fuel, {})
        rating = safe_value(MaxStorage_f.get(fuel))
        tco = (tank_spec.get("cost_per_kWh", 0)
               * max(0, (safe_value(MaxStorage_f.get(fuel)) - tank_spec["existing_rating"]))
               * project_specs["lifetime"] / tank_spec.get("lifetime", 1))
        capex = (max(0, (rating - tank_spec.get("existing_rating", 0)) * tank_spec.get("cost_per_kWh", 0))
                 if fuel == "hydrogen"
                 else max(0, ((rating / fuel_specs.get(fuel, {}).get("kWh/kg", 1)) - tank_spec.get("existing_rating", 0))
                          * tank_spec.get("cost_per_kWh", 0)))
        solution_data["data"].append({
            "category": "tank", "type": fuel, "TCO_1st_installation": tco,
            "rating": rating / fuel_specs.get(fuel, {}).get("kWh/kg"), "capex": capex,
            "lifetime": tank_spec.get("lifetime"),
        })

    # --- META COSTS ---
    try:
        daily_load = df["total_energy_demand"].resample("D").mean().mean()
    except Exception:
        daily_load = df["total_energy_demand"].mean()
    solution_data["meta"] = {
        "costs": {
            "technology_cost": safe_cost(tech_install_cost),
            "storage_cost": safe_cost(storage_cost),
            "grid_cost": None if project_specs.get("offgrid", False) else safe_cost(grid_cost) + service_cost,
            "fixed_cost": safe_cost(fixed_cost),
            "capex": max(0, sum(item.get("capex", 0) for item in solution_data.get("data", []))),
            "LCoE": safe_value(LCoE),
            "LOAD": load_sum,
            "daily_load": daily_load,
        }
    }

    # Build solution dataframe.
    # Pull the entire primal solution vector once (one solver call) and index into it
    # by each variable's column index, instead of calling h.val() per cell (~50k calls).
    col_value = np.asarray(h.getSolution().col_value, dtype=float)

    def _series(varmap, keys):
        """Vectorized lookup of solved values for a list of HiGHS variables."""
        if col_value.size == 0:                       # no primal solution (e.g. infeasible)
            return np.zeros(len(keys))
        return col_value[[varmap[k].index for k in keys]]

    hrs = range(num_timesteps)
    cols = {}
    cols["Power Purchased"] = _series(X_g_h, list(hrs))
    for b in batteries:
        cols[f"State Of Charge - {b}"] = _series(X_se_bh, [(b, hh) for hh in hrs])
    for b in batteries:
        for t in producing_technologies:
            cols[f"{t} to {b}"] = _series(X_pts_bth, [(b, t, hh) for hh in hrs])
    # Explicit solar-to-battery charging column (X_pts_bth for the "solar" technology).
    for b in batteries:
        if "solar" in producing_technologies:
            cols[f"Solar to {b}"] = _series(X_pts_bth, [(b, "solar", hh) for hh in hrs])
    for t in technologies:
        rp = _series(X_rp_th, [(t, hh) for hh in hrs])                          # dispatch variable X_rp_th
        flt = np.array([f_l_t.get((t, hh), 0.0) for hh in hrs], dtype=float)    # availability (GHI*derate for solar)
        # Actual generation delivered to the system = X_rp_th * f_l_t
        cols[f"Power Output - {t}"] = rp * flt

    # Fuel burned by each fuel technology: X_f_th = fuel_burn_rate * X_rp_th (e.g. diesel_fuel by diesel).
    for t in fuel_technologies:
        for f in fuels:
            cols[f"{f}_burned_{t}"] = fuel_burn_rates[(t, f)] * _series(X_rp_th, [(t, hh) for hh in hrs])

    # Explicit solar-to-battery charging column (X_pts_bth for the "solar" technology).
    for b in batteries:
        if "solar" in producing_technologies:
            cols[f"Solar to {b}"] = _series(X_pts_bth, [(b, "solar", hh) for hh in hrs])
    for b in batteries:
        cols[f"{b} to Load"] = _series(X_dfs_bth, [(b, hh) for hh in hrs])
    cols["GHI"] = df["G(i)"].to_numpy()[:num_timesteps]
    cols["LOAD"] = df["total_energy_demand"].to_numpy()[:num_timesteps]

    solution_df = pd.DataFrame(cols, index=range(num_timesteps))

    return solution_data, solution_df, status


