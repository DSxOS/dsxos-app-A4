"""A4 self-consumption maximisation: plans the ESS power so that as little PV energy as possible is exported.

Run by the platform's python-app scheduler with the site's config (default /app/config.yaml).
Requirements: docs/requirements-a4-nrg-integration.md (A4-IMP), docs/requirements-a4-anti-flicker.md (A4-AF),
docs/requirements-opd-operational-datapoints.md (OPD: PCC window, SoC window, charge/discharge power and capacity
are each a datapoint identifier (string) or a number; removed params are configuration errors).
Exit codes: 0 = plan (or fallback plan) posted, 1 = configuration/input error or no plan possible.
"""
import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

import yaml

import model as a4model
import query_utils
import Util
from debug import debug_model
from logger import setup_logger

logger = logging.getLogger("A4Runner")

DEFAULTS = {
    "essChargeEfficiency": 0.95,
    "essDischargeEfficiency": 0.95,
    "essDegradationWeight": 1e-6,
    "essLossWeight": 2.0,
    "essRampWeight": 0.001,
    "pccLimitPenaltyPerKWh": 1000.0,
    "endEnergyPenaltyPerKWh": 10.0,
    "solverTimeLimitS": 300,
    "maxInputAgeS": 900,
    "lokiUrl": "http://localhost:3100/loki/api/v1/push",
    "writeDebugFile": "on-failure",
    "essEffectiveEndCharge": None,
    "essCurrentPowerPlanIdentifier": None,
}
# Operational parameters (OPD R1): a datapoint identifier (string) or a number. Unit of the value.
OPERATIONAL = {
    "pccImportLimitW": "W",             # upper limit of the net PCC power, import positive
    "pccExportLimitW": "W",             # lower limit of the net PCC power, signed (negative = export allowed)
    "ess_min_soc": "fraction",          # SoC limits are fractions 0..1 (SFU D1)
    "ess_max_soc": "fraction",
    "ess_max_P_charge_dp_id": "W",
    "ess_max_P_discharge_dp_id": "W",
    "ess_max_E_dp_id": "Wh",
}
REQUIRED = [
    "apiEndpoint", "clientId", "token",
    "consumptionPrognosisIdentifier", "productionPrognosisIdentifier", "essEffectiveCharge",
    *OPERATIONAL,
    "essResultPowerPlanIdentifier", "interval", "min_period",
]
_P_REPLACEMENT = "use ess_max_P_charge_dp_id and ess_max_P_discharge_dp_id (a datapoint identifier or a number, W, both positive)"
# Removed or replaced params (OPD R5): present in the YAML = configuration error that names the replacement.
REMOVED = {
    "essMinSafeLim": "removed, delete it",
    "npPriceIdentifier": "removed, delete it",
    "ess_max_P_dp_id": "replaced: " + _P_REPLACEMENT,
    "essPowerLimitW": "replaced: " + _P_REPLACEMENT,
    "essMaxCharge": "replaced: use ess_max_E_dp_id (a datapoint identifier, or the capacity as a number in Wh)",
}


class ConfigError(Exception):
    pass


class InputError(Exception):
    pass


def empty(v):
    return v is None or v == ""


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def soc_msg(vals, rule):
    """SFU R1 message: names both SoC parameters with their values; says 'fractions' when a value is > 1."""
    mn, mx = vals.get("ess_min_soc"), vals.get("ess_max_soc")
    fmt = lambda v: "?" if v is None else format(v, "g")
    txt = f"{rule} (ess_min_soc={fmt(mn)}, ess_max_soc={fmt(mx)})"
    for n, v in (("ess_min_soc", mn), ("ess_max_soc", mx)):
        if v is not None and v > 1:
            txt += f"; the SoC limits are fractions: {n}={v:g} looks like percent; use {v / 100:g}"
    return txt


def limit_problems(vals):
    """R2 checks on resolved operational values (a dict name -> number; checks whose params are absent are skipped).

    Returns [(names involved, message)]."""
    p = vals.get
    checks = [
        (("ess_max_P_charge_dp_id",), "ess_max_P_charge_dp_id must be > 0", lambda: p("ess_max_P_charge_dp_id") > 0),
        (("ess_max_P_discharge_dp_id",), "ess_max_P_discharge_dp_id must be > 0",
         lambda: p("ess_max_P_discharge_dp_id") > 0),
        (("ess_max_E_dp_id",), "ess_max_E_dp_id must be > 0", lambda: p("ess_max_E_dp_id") > 0),
        (("ess_min_soc",), soc_msg(vals, "ess_min_soc must be >= 0"), lambda: p("ess_min_soc") >= 0),
        (("ess_max_soc",), soc_msg(vals, "ess_max_soc must be <= 1"), lambda: p("ess_max_soc") <= 1),
        (("ess_min_soc", "ess_max_soc"), soc_msg(vals, "need 0 <= ess_min_soc < ess_max_soc <= 1"),
         lambda: p("ess_min_soc") < p("ess_max_soc")),
        (("pccExportLimitW", "pccImportLimitW"), "need pccExportLimitW <= pccImportLimitW",
         lambda: p("pccExportLimitW") <= p("pccImportLimitW")),
    ]
    return [(names, msg) for names, msg, ok in checks if all(n in vals for n in names) and not ok()]


def load_params(raw):
    """Merge defaults into params and validate (A4-IMP R5, OPD R1/R2/R5/R6). Raises ConfigError."""
    if not isinstance(raw, dict) or not isinstance(raw.get("params"), dict):
        raise ConfigError("config has no 'params' section")
    stale = [f"{k}: {msg}" for k, msg in REMOVED.items() if k in raw["params"]]
    if stale:
        raise ConfigError("removed or replaced params: " + "; ".join(stale))
    params = dict(DEFAULTS)
    params.update(raw["params"])
    if empty(params.get("clientId")) and not empty(params.get("clientID")):
        params["clientId"] = params["clientID"]          # legacy spelling
    missing = [k for k in REQUIRED if empty(params.get(k))]
    if missing:
        raise ConfigError(f"missing params: {', '.join(missing)}")
    bad_type = [k for k in OPERATIONAL if not (is_number(params[k]) or isinstance(params[k], str))]
    if bad_type:
        raise ConfigError(f"{', '.join(bad_type)}: must be a datapoint identifier (string) or a number")
    consts = {k: float(params[k]) for k in OPERATIONAL if is_number(params[k])}
    checks = [(False, msg) for _, msg in limit_problems(consts)]
    checks += [
        (0 < float(params["essChargeEfficiency"]) <= 1, "essChargeEfficiency must be in (0, 1]"),
        (0 < float(params["essDischargeEfficiency"]) <= 1, "essDischargeEfficiency must be in (0, 1]"),
        (float(params["essDegradationWeight"]) >= 0, "essDegradationWeight must be >= 0"),
        (float(params["essLossWeight"]) >= 0, "essLossWeight must be >= 0"),
        (float(params["essRampWeight"]) >= 0, "essRampWeight must be >= 0"),
        (float(params["pccLimitPenaltyPerKWh"]) >= 0, "pccLimitPenaltyPerKWh must be >= 0"),
        (float(params["endEnergyPenaltyPerKWh"]) >= 0, "endEnergyPenaltyPerKWh must be >= 0"),
        (float(params["solverTimeLimitS"]) > 0, "solverTimeLimitS must be > 0"),
        (float(params["maxInputAgeS"]) > 0, "maxInputAgeS must be > 0"),
        (int(params["interval"]) > 0, "interval must be > 0"),
        (int(params["min_period"]) >= 0, "min_period must be >= 0"),
        (params["writeDebugFile"] in ("on-failure", "always", "never"),
         "writeDebugFile must be on-failure, always or never"),
    ]
    errors = [msg for ok, msg in checks if not ok]
    if errors:
        raise ConfigError("; ".join(errors))
    return params


def read_value(identifier, param, now=None, max_age_s=None):
    """Last value of a datapoint, with clear errors. Raises InputError."""
    dp = query_utils.get_datapoint(identifier)
    if dp is None:
        raise InputError(f"reading datapoint '{identifier}' ({param}) failed")
    if not dp:
        raise InputError(f"datapoint '{identifier}' ({param}) does not exist")
    val = dp[0].get("lastReadingValue")
    if val is None:
        raise InputError(f"datapoint '{identifier}' ({param}) has no value")
    if max_age_s is not None:
        last_time = dp[0].get("lastReadingTime")
        if not last_time:
            raise InputError(f"datapoint '{identifier}' ({param}) has no reading time")
        age = (now - Util.parse_time(last_time)).total_seconds()
        if age > max_age_s:
            raise InputError(f"datapoint '{identifier}' ({param}) is {int(age)} s old (max {int(max_age_s)} s)")
    return float(val)


def value_or_datapoint(params, name):
    """Operational param (OPD R1): a string is a datapoint (latest value, read now), a number is a constant.

    Returns (value in the param's unit, source text 'dp <id>' or 'const'). Raises InputError."""
    v = params[name]
    if isinstance(v, str):
        return read_value(v, name), f"dp {v}"
    return float(v), "const"


def resolve_limits(params):
    """Resolve all operational params once per run and check them (OPD R1, R2). Raises InputError."""
    vals, src = {}, {}
    for name in OPERATIONAL:
        vals[name], src[name] = value_or_datapoint(params, name)
    for names, msg in limit_problems(vals):
        if any(src[n] != "const" for n in names):       # all-const violations were configuration errors already
            detail = ", ".join(f"{n}={vals[n]:g} ({src[n]})" for n in names)
            raise InputError(f"{msg}: {detail}")
    return vals, src


def to_total(param, usable_kwh, capacity_kwh, smin, smax):
    """SFU R3: usable energy above ess_min_soc -> total stored energy (kWh); WARNING when clamped."""
    total, clamped = a4model.usable_to_total_kwh(usable_kwh, capacity_kwh, smin, smax)
    if clamped != usable_kwh:
        logger.warning(f"{param}: usable energy {usable_kwh:.3f} kWh is outside 0..{capacity_kwh * (smax - smin):.3f} kWh "
                       f"(capacity x (ess_max_soc - ess_min_soc)); clamped to {clamped:.3f} kWh")
    return total


def read_forecast(identifier, param):
    readings = query_utils.get_last_prognosis_readings(identifier)
    if readings is None:
        raise InputError(f"reading the forecast of '{identifier}' ({param}) failed")
    return [{"time": r["time"], "value": r["value"]} for r in readings if r.get("value") is not None]


def build_series(load, pv, start, interval_s, min_period_s):
    """Common time grid from start to the end of the shortest forecast. Returns (times, load_kw, pv_kw) or None."""
    if not load or not pv:
        logger.warning(f"forecast missing: load={len(load)}, pv={len(pv)} values")
        return None
    end = Util.parse_time(Util.find_common_time_range([load, pv])["end"])
    if (end - start).total_seconds() < min_period_s:
        logger.warning(f"common forecast horizon {start.isoformat()} .. {end.isoformat()} is shorter than "
                       f"min_period {min_period_s} s")
        return None
    try:
        load_s = Util.extract_prognosis_values(load, "consumption", start, end, interval_s)
        pv_s = Util.extract_prognosis_values(pv, "production", start, end, interval_s)
    except (Util.TaskFailException, ValueError) as e:
        logger.warning(f"forecast cannot cover the planning window: {e}")
        return None
    n = min(len(load_s), len(pv_s))
    times = [start + timedelta(seconds=k * interval_s) for k in range(n)]
    return times, [r["value"] / 1000.0 for r in load_s[:n]], [r["value"] / 1000.0 for r in pv_s[:n]]


def iso(t):
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def run_fallback(params, start, result_dp_id, reason):
    """Re-post the remaining part of the previous plan (A5-IMP R11). Returns the exit code."""
    source = params.get("essCurrentPowerPlanIdentifier") or params["essResultPowerPlanIdentifier"]
    previous = query_utils.get_last_prognosis_readings(source)
    remaining = [r for r in (previous or [])
                 if r.get("value") is not None and Util.parse_time(r["time"]) >= start]
    if not remaining:
        logger.error(f"{reason}; no previous plan in '{source}' to fall back on - nothing posted.")
        return 1
    readings = [{"time": r["time"], "value": float(r["value"])} for r in remaining]
    query_utils.post_datapoint_prognosis(result_dp_id, iso(start), readings)
    logger.warning(f"{reason}; posted the fallback plan ({len(readings)} values from '{source}').")
    return 0


def run(config_path, now=None):
    with open(config_path, "r") as f:
        raw = yaml.safe_load(f)
    app_name = raw.get("appModule", "a4") if isinstance(raw, dict) else "a4"
    loki_url = (raw.get("params") or {}).get("lokiUrl", DEFAULTS["lokiUrl"]) if isinstance(raw, dict) else None
    setup_logger(app_name="A4Runner", log_file="query.log", loki_url=loki_url, loki_tags={"app_name": app_name})
    logger.info(f"{app_name} start.")

    try:
        params = load_params(raw)
    except (ConfigError, ValueError, TypeError) as e:
        logger.error(f"configuration error: {e}")
        return 1

    query_utils.init(params["apiEndpoint"], {})
    jwt_token = query_utils.get_token(params["clientId"], params["token"])
    if not jwt_token:
        logger.error(f"{app_name}: could not obtain an API token from {params['apiEndpoint']}/auth/token "
                     f"for clientId '{params['clientId']}' (check the credential in the app config). Stopping.")
        return 1
    query_utils.add_header("Authorization", f"Bearer {jwt_token}")

    now = now or datetime.now(timezone.utc)
    interval_s = int(params["interval"])
    start = Util.align_start(now, interval_s)

    try:
        result_dp = query_utils.get_datapoint(params["essResultPowerPlanIdentifier"])
        if not result_dp:
            raise InputError(f"result datapoint '{params['essResultPowerPlanIdentifier']}' does not exist")
        result_dp_id = result_dp[0]["id"]
        lim, lim_src = resolve_limits(params)
        capacity_kwh = lim["ess_max_E_dp_id"] / 1000.0
        smin, smax = lim["ess_min_soc"], lim["ess_max_soc"]
        u0_kwh = read_value(params["essEffectiveCharge"], "essEffectiveCharge",
                            now=now, max_age_s=float(params["maxInputAgeS"])) / 1000.0
        e0_kwh = to_total("essEffectiveCharge", u0_kwh, capacity_kwh, smin, smax)
        end_id = params.get("essEffectiveEndCharge")
        if end_id:
            u_end_kwh = read_value(end_id, "essEffectiveEndCharge") / 1000.0
            e_end_kwh = to_total("essEffectiveEndCharge", u_end_kwh, capacity_kwh, smin, smax)
        else:
            u_end_kwh, e_end_kwh = None, e0_kwh        # default end target = the converted start energy (SFU R3)
        load = read_forecast(params["consumptionPrognosisIdentifier"], "consumptionPrognosisIdentifier")
        pv = read_forecast(params["productionPrognosisIdentifier"], "productionPrognosisIdentifier")
    except InputError as e:
        logger.error(f"input error: {e}")
        return 1

    limits_txt = ", ".join(f"{n}={lim[n]:g} {OPERATIONAL[n]} ({lim_src[n]})" for n in OPERATIONAL)
    logger.info(f"inputs: {limits_txt}, forecast values load/pv={len(load)}/{len(pv)}, start={iso(start)}")
    end_txt = ("usable end target = start (default)" if u_end_kwh is None else f"usable end target={u_end_kwh:.3f} kWh")
    logger.info(f"resolved inputs: ess_min_soc={smin:g}, ess_max_soc={smax:g} (fractions), capacity={capacity_kwh:.3f} kWh, "
                f"usable start energy={u0_kwh:.3f} kWh, total start energy={e0_kwh:.3f} kWh, {end_txt}, "
                f"total end target={e_end_kwh:.3f} kWh, "
                f"charge limit={lim['ess_max_P_charge_dp_id'] / 1000.0:g} kW, "
                f"discharge limit={lim['ess_max_P_discharge_dp_id'] / 1000.0:g} kW, "
                f"PCC limits={lim['pccExportLimitW'] / 1000.0:g}..{lim['pccImportLimitW'] / 1000.0:g} kW")

    series = build_series(load, pv, start, interval_s, int(params["min_period"]))
    if series is None:
        return run_fallback(params, start, result_dp_id, "not enough forecast data")
    times, load_s, pv_s = series

    inp = a4model.PlanInputs(
        interval_s=interval_s, load_kw=load_s, pv_kw=pv_s,
        ch_max_kw=lim["ess_max_P_charge_dp_id"] / 1000.0, dis_max_kw=lim["ess_max_P_discharge_dp_id"] / 1000.0,
        capacity_kwh=capacity_kwh, e0_kwh=e0_kwh, e_end_kwh=e_end_kwh,
        soc_min_frac=smin, soc_max_frac=smax,
        import_limit_kw=lim["pccImportLimitW"] / 1000.0,
        pcc_min_kw=lim["pccExportLimitW"] / 1000.0,
        eta_charge=float(params["essChargeEfficiency"]), eta_discharge=float(params["essDischargeEfficiency"]),
        degradation_weight=float(params["essDegradationWeight"]),
        loss_weight=float(params["essLossWeight"]),
        ramp_weight=float(params["essRampWeight"]),
        pcc_penalty_per_kwh=float(params["pccLimitPenaltyPerKWh"]),
        end_penalty_per_kwh=float(params["endEnergyPenaltyPerKWh"]),
    )
    m = a4model.build_model(inp)
    result = a4model.solve(m, time_limit_s=float(params["solverTimeLimitS"]))
    if params["writeDebugFile"] == "always" or (params["writeDebugFile"] == "on-failure" and not result.ok):
        debug_model(m)
    if not result.ok:
        return run_fallback(params, start, result_dp_id, f"solver result {result.status}")

    logger.info(f"solved ({result.status}): {len(times)} steps {iso(times[0])} .. {iso(times[-1])}, "
                f"export {result.export_kwh:.2f} kWh, losses {result.loss_kwh:.2f} kWh, ramp term {result.ramp_objective:.4f}, end energy {result.energy_kwh[-1]:.1f} kWh")
    if result.import_overrun_kwh > 1e-6 or result.below_limit_kwh > 1e-6:
        logger.warning(f"plan violates the PCC window: import {result.import_overrun_kwh:.2f} kWh over the upper limit, "
                       f"{result.below_limit_kwh:.2f} kWh below the lower limit over the horizon")
    if abs(result.end_deviation_kwh) > 1e-6:
        logger.warning(f"end energy misses its target by {result.end_deviation_kwh:.2f} kWh")
    readings = [{"time": iso(t), "value": float(v * 1000.0)} for t, v in zip(times, result.ess_kw)]
    query_utils.post_datapoint_prognosis(result_dp_id, iso(start), readings)
    logger.info(f"{app_name} finished: plan posted to '{params['essResultPowerPlanIdentifier']}'.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run A4 with config file")
    parser.add_argument("-c", "--config", required=False, help="Path to config YAML file", default="/app/config.yaml")
    args = parser.parse_args(argv)
    try:
        return run(args.config)
    except Exception:  # last resort: a clear message instead of a bare traceback
        logger.exception("A4 failed with an unexpected error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
