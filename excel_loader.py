"""Turn the inputs workbook (Google Sheet or local .xlsx) into the spec dicts the solver expects.

Cost classification (this is the rule to follow when adding BOS rows):
    per_kW   -> folded into solar  cost_per_kW    (changes sizing)
    per_kWh  -> folded into battery cost_per_kWh   (changes sizing)
    fixed    -> project_specs["fixed_cost"]        (changes LCoE only)
    report_only -> ignored by the model (BOM only)

The solar derate (panel.derating * inverter.efficiency) is computed once here and applied
later in f_l_t, so inverter efficiency is never double-counted.
"""
import io
import re
import urllib.error
import urllib.request
from types import SimpleNamespace

import pandas as pd

# NOTE: default data source. Pass a local path to load_inputs("inputs.xlsx") to use a file.
DEFAULT_SOURCE = "https://docs.google.com/spreadsheets/d/1Crre-82kURC6sVCwFSJgN03BnGRC3EOMqF8pgb8Q-O0/edit?gid=957556041#gid=957556041"

# Each sheet and the columns it must contain.
REQUIRED = {
    "solar_panels": ["name", "rating_W", "cost_per_kW", "derating", "lifetime", "maintenance_per_kW"],
    "batteries":    ["name", "module_kWh", "cost_per_kWh", "efficiency", "dod", "c_rate", "lifetime", "maintenance_per_kWh"],
    "inverters":    ["name", "rating_kW", "cost_per_kW", "efficiency", "lifetime"],
    "bos":          ["item", "basis", "unit_cost", "qty", "applies_to", "notes"],
}
# Selections + project params come from a 'setup' key/value sheet (preferred), or the legacy
# 'selection' + 'project' sheets. Diesel/tank/fuel params live alongside them and are optional.
VALID_BASIS = {"per_kW", "per_kWh", "fixed", "report_only"}

# Inverter sizing rule: inverter_kW = max(INVERTER_SOLAR_RATIO * solar_kW, INVERTER_PEAK_FACTOR * peak_demand).
# Only the per-solar part scales with sizing, so the optimizer carries that fraction of the
# inverter $/kW in eff_solar_cost_per_kW; the peak-demand floor is added in the BOM (run_microgrid).
INVERTER_SOLAR_RATIO = 0.5
INVERTER_PEAK_FACTOR = 1.25   # inverter must be at least 1.25 x the peak demand

# PVGIS TMY site used when the project sheet has no (valid) lat/lon.
DEFAULT_LAT, DEFAULT_LON = -3.314732, 37.326358

# Tariff rates in the 'tariff' sheet are in TZS; divide by this to get USD.
TZS_PER_USD = 2650.0

# Fixed parameters (hardcoded; not editable from the sheet).
FIXED_RESILIENCY_DAYS = 0
FIXED_DAY_HOURS = 24.0
FIXED_MAX_DIESEL = 0.5
FIXED_TANK_FREQUENCY_HOURS = 24
FIXED_DIESEL_ENERGY_KWH_PER_L = 10.1     # ~11.9 kWh/kg × 0.85 kg/L (diesel density)


class InputError(ValueError):
    """Malformed or inconsistent inputs (file or Google Sheet)."""


def _is_url(s):
    return isinstance(s, str) and s.lower().startswith(("http://", "https://"))


def _gsheet_xlsx_url(url):
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", url)
    return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=xlsx" if m else url


def _download_xlsx(url):
    req = urllib.request.Request(_gsheet_xlsx_url(url), headers={"User-Agent": "Mozilla/5.0"})
    try:
        data = urllib.request.urlopen(req, timeout=60).read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise InputError(f"Sheet not publicly readable (HTTP {e.code}). Share -> 'Anyone with the link' -> Viewer.")
        raise InputError(f"Could not download spreadsheet: HTTP {e.code}.")
    except Exception as e:
        raise InputError(f"Could not download spreadsheet: {e}")
    if data[:2] != b"PK":  # a real .xlsx is a zip; an HTML sign-in page is not
        raise InputError("Downloaded content is not an .xlsx (likely a permission page). Share the sheet as Viewer.")
    return data


def fetch_to_local(source=DEFAULT_SOURCE, dst="inputs_download.xlsx"):
    """Download a URL source to a local .xlsx and return its path (sanity_check needs an editable copy)."""
    if not _is_url(source):
        return source
    with open(dst, "wb") as f:
        f.write(_download_xlsx(source))
    return dst


def _read_sheets(source):
    if _is_url(source):
        sheets = pd.read_excel(io.BytesIO(_download_xlsx(source)), sheet_name=None, engine="openpyxl")
    else:
        try:
            sheets = pd.read_excel(source, sheet_name=None, engine="openpyxl")
        except FileNotFoundError:
            raise InputError(f"Workbook not found: {source!r}.")
    for name, cols in REQUIRED.items():
        if name not in sheets:
            raise InputError(f"Missing sheet '{name}'. Found: {list(sheets)}.")
        missing = [c for c in cols if c not in sheets[name].columns]
        if missing:
            raise InputError(f"Sheet '{name}' is missing columns: {missing}.")
    if "setup" not in sheets and not {"selection", "project"}.issubset(sheets):
        raise InputError("Need a 'setup' sheet, or both legacy 'selection' and 'project' sheets.")
    return sheets


def _pick(catalog, sheet_name, name):
    """The single catalog row whose 'name' matches; clear error if absent."""
    hits = catalog[catalog["name"].astype(str) == str(name)]
    if len(hits) == 0:
        raise InputError(f"selection '{name}' is not a row in '{sheet_name}'. "
                         f"Available: {', '.join(catalog['name'].astype(str))}.")
    return hits.iloc[0].to_dict()


def _sel_get(sel, key):
    """The chosen name for a component, or None if the selection cell is blank."""
    v = str(sel.get(key, "")).strip()
    return None if v.lower() in ("", "nan", "none") else v


def _read_config(sheets):
    """Return (selections, params) from the 'setup' key/value sheet (columns A=key, B=value),
    or from the legacy 'selection' + 'project' sheets."""
    if "setup" in sheets:
        df = sheets["setup"]
        kv = {}
        for _, row in df.iterrows():
            key = str(row.iloc[0]).strip()
            if key and key.lower() != "nan" and len(row) > 1:
                kv[key] = row.iloc[1]
        sel = {c: kv.get(c) for c in ("solar_panel", "battery", "inverter", "diesel", "tariff")}
        return sel, kv
    sel = {str(r["component"]): r["choice"] for _, r in sheets["selection"].iterrows()}
    return sel, sheets["project"].iloc[0].to_dict()


def load_inputs(path=DEFAULT_SOURCE):
    sheets = _read_sheets(path)

    # Selections + project params come from the 'setup' sheet (or legacy selection+project).
    # A blank component 'choice' leaves that component out of the model.
    sel, p = _read_config(sheets)
    panel_name = _sel_get(sel, "solar_panel")
    battery_name = _sel_get(sel, "battery")
    inverter_name = _sel_get(sel, "inverter")
    has_solar = panel_name is not None
    has_battery = battery_name is not None
    has_diesel = _sel_get(sel, "diesel") is not None   # any non-blank diesel choice enables it
    if has_solar and inverter_name is None:
        raise InputError("solar_panel is selected but inverter is blank; solar needs an inverter.")
    panel = _pick(sheets["solar_panels"], "solar_panels", panel_name) if has_solar else None
    inverter = _pick(sheets["inverters"], "inverters", inverter_name) if has_solar else None
    battery = _pick(sheets["batteries"], "batteries", battery_name) if has_battery else None

    # BOS lines, with guards against double-counting / misclassification
    bos = sheets["bos"].copy()
    bos["basis"] = bos["basis"].astype(str).str.strip()
    bos["applies_to"] = bos["applies_to"].astype(str).str.strip()
    bad = sorted(set(bos["basis"]) - VALID_BASIS)
    if bad:
        raise InputError(f"bos.basis has invalid value(s) {bad}; allowed: {sorted(VALID_BASIS)}.")
    if bos["item"].astype(str).str.contains("inverter", case=False).any():
        raise InputError("Inverter cost comes from the inverters sheet; remove any 'inverter' BOS line.")
    for _, r in bos.iterrows():
        if r["basis"] == "per_kW" and r["applies_to"] != "solar":
            raise InputError(f"BOS '{r['item']}' is per_kW but applies_to != 'solar'.")
        if r["basis"] == "per_kWh" and r["applies_to"] != "battery":
            raise InputError(f"BOS '{r['item']}' is per_kWh but applies_to != 'battery'.")

    def _bos_sum(basis, applies_to=None):
        m = bos["basis"] == basis
        if applies_to is not None:
            m &= bos["applies_to"] == applies_to
        return float((bos.loc[m, "unit_cost"] * bos.loc[m, "qty"]).sum())

    per_kw_solar = _bos_sum("per_kW", "solar")
    per_kwh_batt = _bos_sum("per_kWh", "battery")
    fixed_cost = _bos_sum("fixed")

    # Effective (sizing-driving) costs and one-time derate, only for the selected components.
    # Inverter scales at INVERTER_SOLAR_RATIO of solar, so only that share of its $/kW is in eff_solar.
    if has_solar:
        eff_solar_cost_per_kW = (float(panel["cost_per_kW"])
                                 + INVERTER_SOLAR_RATIO * float(inverter["cost_per_kW"]) + per_kw_solar)
        derate = float(panel["derating"]) * float(inverter["efficiency"])
    else:
        eff_solar_cost_per_kW, derate = 0.0, 1.0
    eff_battery_cost_per_kWh = (float(battery["cost_per_kWh"]) + per_kwh_batt) if has_battery else 0.0

    # An optional separate 'diesel' sheet can still augment the params (legacy support).
    if "diesel" in sheets and len(sheets["diesel"]) > 0:
        p.update(sheets["diesel"].iloc[0].to_dict())

    def pget(key, default):
        v = p.get(key, default)
        return default if pd.isna(v) else v

    grid_capacity = float(p["grid_capacity_kW"])

    # Grid energy price ALWAYS comes from the selected tariff (rows in 'tariff' are TZS -> USD).
    tariff_name = _sel_get(sel, "tariff")
    if tariff_name is None:
        raise InputError("Select a 'tariff' in setup — grid pricing comes from the 'tariff' sheet.")
    if "tariff" not in sheets:
        raise InputError("Missing 'tariff' sheet (grid pricing comes from it).")
    tr = _pick(sheets["tariff"], "tariff", tariff_name)

    def _trf(key, default=0.0):
        v = tr.get(key, default)
        return float(default if pd.isna(v) else v)

    tariff_spec = {
        "name": tariff_name,
        "energy_rate": _trf("energy_TZS_per_kWh") / TZS_PER_USD,            # $/kWh (above block)
        "block_kWh": _trf("block_kWh"),                                     # monthly tier threshold
        "block_rate": _trf("block_rate_TZS") / TZS_PER_USD,                # $/kWh (below block)
        "service_per_month": _trf("service_TZS_per_month") / TZS_PER_USD,  # $/month fixed
        "demand_per_kW_month": _trf("demand_TZS_per_kWp_month") / TZS_PER_USD,  # $/kW-peak/month
    }
    grid_price = tariff_spec["energy_rate"]   # used for the df 'tariff' column / reporting

    grid_max_frac = float(pget("grid_max_fraction", 0.20))   # NOTE: < 1 makes grid limit bind; drives sizing
    solar_max_kW = float(pget("solar_max_kW", 100000))
    battery_max_kWh = float(pget("battery_max_kWh", 100000))
    solar_min_kW = float(pget("solar_min_kW", 0))          # minimum sizing limits (force at least this much)
    battery_min_kWh = float(pget("battery_min_kWh", 0))
    if solar_min_kW > solar_max_kW:
        raise InputError(f"solar_min_kW ({solar_min_kW}) > solar_max_kW ({solar_max_kW}).")
    if battery_min_kWh > battery_max_kWh:
        raise InputError(f"battery_min_kWh ({battery_min_kWh}) > battery_max_kWh ({battery_max_kWh}).")
    # Operational battery DoD floor (minimum SoC) — an independent Setup input, NOT the battery
    # catalogue spec. Months Apr-Sep ("around July") use battery_dod_jul; Oct-Mar use battery_dod_jan.
    battery_dod_jan = float(pget("battery_dod_jan", 0.2))
    battery_dod_jul = float(pget("battery_dod_jul", 0.2))
    day_hours = FIXED_DAY_HOURS
    max_diesel = FIXED_MAX_DIESEL
    lat = float(pget("lat", DEFAULT_LAT))                    # NOTE: PVGIS TMY site; add 'lat'/'lon' to project sheet
    lon = float(pget("lon", DEFAULT_LON))
    # Guard: a sheet locale that treats '.' as a thousands separator turns -3.314732 into
    # -3314732. If coords land out of range, warn and fall back to the defaults.
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        print(f"WARNING: project lat/lon ({lat}, {lon}) out of range — check the sheet's number "
              f"format. Using default ({DEFAULT_LAT}, {DEFAULT_LON}).")
        lat, lon = DEFAULT_LAT, DEFAULT_LON

    # Load from the optional 'load' sheet. Accepted formats:
    #   full year     : a 'kWh' column with 8760 values (no 'hour') -> used directly, no timestamp
    #   1-day profile : 'hour' (0-23) + 'kWh' (24 rows)             -> tiled to every day
    #   flat          : 'name' + 'kWh_per_day'                      -> summed, spread evenly
    # `load_hourly` (full series) takes priority in run_microgrid; otherwise the 24-value
    # `load_day_profile` is tiled to 8760 h.
    load_hourly = None
    if ("load" in sheets and {"hour", "kWh"}.issubset(sheets["load"].columns)
            and len(sheets["load"]) == 24):
        # 24-row daily profile, tiled to every day
        ld = sheets["load"].sort_values("hour")
        load_day_profile = [float(v) for v in ld["kWh"]]
        load_components = ld.to_dict("records")
    elif "load" in sheets and "kWh" in sheets["load"].columns:
        # full hourly series (8760) used directly, in row order — an 'hour' column is ignored here
        vals = pd.to_numeric(sheets["load"]["kWh"], errors="coerce").dropna()
        load_hourly = [float(v) for v in vals]
        if len(load_hourly) < 24:
            raise InputError(f"'load' sheet 'kWh' column has only {len(load_hourly)} values; expected a full series (8760).")
        load_day_profile = load_hourly[:24]
        load_components = []
    elif "load" in sheets and {"name", "kWh_per_day"}.issubset(sheets["load"].columns):
        load_components = sheets["load"].to_dict("records")
        load_day_profile = [float(sheets["load"]["kWh_per_day"].sum()) / 24.0] * 24
    else:
        load_components = []
        load_day_profile = [235.0] * 24

    _load_series = load_hourly if load_hourly is not None else load_day_profile
    flat_load_kWh_per_h = float(sum(_load_series)) / len(_load_series)
    daily_load_kWh = flat_load_kWh_per_h * 24.0
    peak_load_kWh_per_h = max(_load_series)

    # Peak demand (kW) for inverter sizing: explicit 'peak_demand' column in the 'load' sheet,
    # else fall back to the profile's peak hour. The inverter must be >= 1.25 x this peak.
    peak_demand_kW = peak_load_kWh_per_h
    if "load" in sheets and "peak_demand" in sheets["load"].columns:
        vals = pd.to_numeric(sheets["load"]["peak_demand"], errors="coerce").dropna()
        if len(vals):
            peak_demand_kW = float(vals.max())
    inverter_min_kW = INVERTER_PEAK_FACTOR * peak_demand_kW

    # Spec dicts — only the selected components go into sets/subsets (what the solver iterates).
    tech_specs, storage_specs = {}, {}
    if has_solar:
        tech_specs["solar"] = {"size": None, "min_output": solar_min_kW, "max_output": solar_max_kW,
                               "cost_per_kW": eff_solar_cost_per_kW,
                               "maintenance_per_kW": float(panel["maintenance_per_kW"]),
                               "existing_rating": 0, "lifetime": int(panel["lifetime"]),
                               "efficiency": 1.0, "module_capacity": float(panel["rating_W"]) / 1000.0}
    if has_battery:
        storage_specs["li"] = {"size": None, "min_capacity": battery_min_kWh, "max_capacity": battery_max_kWh,
                               "cost_per_kWh": eff_battery_cost_per_kWh,
                               "maintenance_per_kWh": float(battery["maintenance_per_kWh"]),
                               "existing_rating": 0, "lifetime": int(battery["lifetime"]),
                               "efficiency": float(battery["efficiency"]), "dod": float(battery["dod"]),
                               "c_value": float(battery["c_rate"]), "module_capacity": float(battery["module_kWh"])}

    # Diesel generator (only when selected): a fuel-burning technology backed by a fuel tank.
    # The 'diesel' choice names a model in the 'diesel_generators' catalogue (cols: name,
    # cost_per_kW, efficiency, max_kW, lifetime, [maintenance_per_kW]); without a catalogue it
    # falls back to diesel_* params in 'setup'.
    diesel_eff = float(pget("diesel_efficiency", 0.35))
    if has_diesel:
        gen = (_pick(sheets["diesel_generators"], "diesel_generators", _sel_get(sel, "diesel"))
               if "diesel_generators" in sheets else None)
        if gen is not None:
            diesel_eff = float(gen["efficiency"])
            d_cost, d_max, d_life = float(gen["cost_per_kW"]), float(gen["max_kW"]), int(gen["lifetime"])
            d_maint = float(gen.get("maintenance_per_kW", 0) or 0)
        else:
            d_cost, d_max = float(pget("diesel_cost_per_kW", 500)), float(pget("diesel_max_kW", 100000))
            d_life, d_maint = int(pget("diesel_lifetime", 15)), 0.0
        d_min = float(pget("diesel_min_kW", 0))
        if d_min > d_max:
            raise InputError(f"diesel_min_kW ({d_min}) > diesel max ({d_max}).")
        tech_specs["diesel"] = {"size": None, "min_output": d_min, "max_output": d_max,
                                "cost_per_kW": d_cost, "maintenance_per_kW": d_maint, "existing_rating": 0,
                                "lifetime": d_life, "efficiency": diesel_eff, "module_capacity": 10}
    tank_specs = {"diesel_fuel": {"size": None, "min_capacity": float(pget("tank_min_kWh", 0)),
                                  "max_capacity": float(pget("tank_max_kWh", 5000)), "cost_per_kWh": 1,
                                  "maintenance_per_kW": 0, "existing_rating": 0, "lifetime": 20,
                                  "dod": float(pget("tank_dod", 0.1)),
                                  "frequency_hours": FIXED_TANK_FREQUENCY_HOURS, "module_capacity": 100}}
    # Diesel fuel price: input is $/L, convert to $/kWh using FIXED_DIESEL_ENERGY_KWH_PER_L
    fuel_usd_per_L = float(pget("fuel_usd_per_L", 1.5))   # typical diesel ~1.5 $/L
    fuel_specs = {"diesel_fuel": {"$/kWh": fuel_usd_per_L / FIXED_DIESEL_ENERGY_KWH_PER_L,
                                  "kWh/kg": float(pget("fuel_kWh_per_kg", 11.9))}}
    grid_specs = {"grid_capacity": grid_capacity}
    project_specs = {"lifetime": int(p["lifetime"]), "fixed_cost": fixed_cost,
                     "resiliency_days": FIXED_RESILIENCY_DAYS, "day_hours": day_hours,
                     "offgrid": False, "offgrid_base": False, "max_diesel": max_diesel,
                     "max_grid_fraction": grid_max_frac}

    technologies = (["solar"] if has_solar else []) + (["diesel"] if has_diesel else [])
    batteries = ["li"] if has_battery else []
    fuels = ["diesel_fuel"] if has_diesel else []
    fuel_techs = ["diesel"] if has_diesel else []
    sets = {"technologies": technologies, "batteries": batteries, "fuels": fuels}
    subsets = {"purchasing_fuels": list(fuels), "fuel_technologies": list(fuel_techs),
               "producing_technologies": list(technologies),  # solar and diesel can both charge the battery
               "fuel_consuming_technologies": list(fuel_techs), "fuel_creating_technologies": [],
               "diesel_technologies": list(fuel_techs)}
    fuel_burn_rates = {("diesel", "diesel_fuel"): 1.0 / diesel_eff}

    return SimpleNamespace(
        tech_specs=tech_specs, storage_specs=storage_specs, tank_specs=tank_specs,
        fuel_specs=fuel_specs, grid_specs=grid_specs, project_specs=project_specs,
        sets=sets, subsets=subsets, fuel_burn_rates=fuel_burn_rates,
        derate=derate, grid_price=grid_price, lat=lat, lon=lon,
        eff_solar_cost_per_kW=eff_solar_cost_per_kW, eff_battery_cost_per_kWh=eff_battery_cost_per_kWh,
        per_kw_solar=per_kw_solar, per_kwh_batt=per_kwh_batt, fixed_cost=fixed_cost,
        flat_load_kWh_per_h=flat_load_kWh_per_h, peak_load_kWh_per_h=peak_load_kWh_per_h,
        peak_demand_kW=peak_demand_kW, inverter_min_kW=inverter_min_kW, tariff_spec=tariff_spec,
        battery_dod_jan=battery_dod_jan, battery_dod_jul=battery_dod_jul,
        daily_load_kWh=daily_load_kWh, load_day_profile=load_day_profile, load_hourly=load_hourly,
        load_components=load_components,
        panel=panel, battery=battery, inverter=inverter, has_solar=has_solar, has_battery=has_battery,
        bos=bos.to_dict("records"), selection=sel,
    )


def build_optimizer_input(inputs, df, num_timesteps):
    """SimpleNamespace for optimize_microgrid. f_l_t = GHI * derate (derate applied once, here)."""
    f_l_t = {}
    techs = inputs.sets["technologies"]
    if "solar" in techs:
        ghi = df["G(i)"].to_numpy()
        for hh in range(num_timesteps):
            f_l_t[("solar", hh)] = float(ghi[hh]) * inputs.derate
    if "diesel" in techs:                       # diesel is always available (availability = 1)
        for hh in range(num_timesteps):
            f_l_t[("diesel", hh)] = 1.0

    # Per-hour battery SoC floor: months Apr-Sep use the July-season DoD, Oct-Mar the January one.
    soc_floor_hour = None
    if "li" in inputs.storage_specs:
        month_hours = [744, 672, 744, 720, 744, 720, 744, 744, 720, 744, 720, 744]
        jul_season = {4, 5, 6, 7, 8, 9}
        floors = []
        for month, hours in enumerate(month_hours, start=1):
            dod = inputs.battery_dod_jul if month in jul_season else inputs.battery_dod_jan
            floors.extend([dod] * hours)
        floors = floors[:num_timesteps]
        while len(floors) < num_timesteps:                  # horizon beyond one year
            floors.append(floors[-1] if floors else inputs.battery_dod_jan)
        soc_floor_hour = floors

    return SimpleNamespace(
        storage_specs=inputs.storage_specs, project_specs=inputs.project_specs,
        tank_specs=inputs.tank_specs, grid_cut_hours=[], num_timesteps=num_timesteps,
        tech_specs=inputs.tech_specs, fuel_specs=inputs.fuel_specs, grid_specs=inputs.grid_specs,
        df=df, fuel_burn_rates=inputs.fuel_burn_rates, f_l_t=f_l_t,
        sets=inputs.sets, subsets=inputs.subsets, tariff_spec=getattr(inputs, "tariff_spec", None),
        soc_floor_hour=soc_floor_hour,
    )
