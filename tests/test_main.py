"""A4-IMP AC1(a), (f): end to end through main.run with a fake data server."""
from datetime import datetime, timedelta, timezone

import pytest
import yaml

import main
import model as a4model
import query_utils

NOW = datetime(2026, 11, 14, 10, 7, 30, tzinfo=timezone.utc)
START = datetime(2026, 11, 14, 10, 0, 0, tzinfo=timezone.utc)   # aligned to 900 s


def iso(t):
    return t.isoformat().replace("+00:00", "Z")


def series(hours, value, step_min=1, begin=START - timedelta(minutes=30)):
    return [{"time": iso(begin + timedelta(minutes=step_min * i)), "value": value(i)}
            for i in range(int(hours * 60 / step_min) + 1)]


class FakeServer:
    def __init__(self, hours=10):
        self.datapoints = {
            "ess_p_a4_test": {"id": 531},
            "pmax": {"lastReadingValue": 20000.0},
            "cap": {"lastReadingValue": 100000.0},
            "charge": {"lastReadingValue": 50000.0, "lastReadingTime": iso(NOW - timedelta(seconds=30))},
        }
        self.forecasts = {
            "cons": series(hours, lambda i: 10000.0),
            "prod": series(hours, lambda i: -30000.0 if 120 <= i < 240 else 0.0),
            "ess_p_a4_test": [],
        }
        self.posted = []

    def install(self, monkeypatch, token="T"):
        monkeypatch.setattr(query_utils, "get_token", lambda cid, secret: token)
        monkeypatch.setattr(query_utils, "get_datapoint",
                            lambda ident: [self.datapoints[ident]] if ident in self.datapoints else [])
        monkeypatch.setattr(query_utils, "get_last_prognosis_readings",
                            lambda ident, page_size=10000: [dict(r) for r in self.forecasts.get(ident, [])])
        monkeypatch.setattr(query_utils, "post_datapoint_prognosis",
                            lambda dp_id, t, readings: self.posted.append((dp_id, t, readings)) or {"id": 1})


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    params = {
        "apiEndpoint": "http://x/api", "clientId": "c", "token": "s", "lokiUrl": "",
        "consumptionPrognosisIdentifier": "cons", "productionPrognosisIdentifier": "prod",
        "ess_max_P_charge_dp_id": "pmax", "ess_max_P_discharge_dp_id": "pmax", "ess_max_E_dp_id": "cap",
        "essEffectiveCharge": "charge",
        "pccImportLimitW": 170000, "pccExportLimitW": 0,
        "essResultPowerPlanIdentifier": "ess_p_a4_test", "interval": 900, "min_period": 21600,
        "solverTimeLimitS": 60,
    }
    path = tmp_path / "config.yaml"

    def write(drop=(), **over):
        p = dict(params, **over)
        for k in drop:
            p.pop(k, None)
        path.write_text(yaml.safe_dump({"appModule": "a4", "params": p}))
        return str(path)
    return write


def test_plan_is_posted_on_an_aligned_grid(cfg, monkeypatch):
    srv = FakeServer()
    srv.install(monkeypatch)
    assert main.run(cfg(), now=NOW) == 0
    dp_id, t, readings = srv.posted[0]
    assert dp_id == 531 and t == "2026-11-14T10:00:00Z"
    assert readings[0]["time"] == "2026-11-14T10:00:00Z" and readings[1]["time"] == "2026-11-14T10:15:00Z"


def test_static_limits_are_used_when_no_datapoints(cfg, monkeypatch, caplog):
    srv = FakeServer()
    srv.install(monkeypatch)
    path = cfg(ess_max_P_charge_dp_id=50000, ess_max_P_discharge_dp_id=30000, ess_max_E_dp_id=200000)
    assert main.run(path, now=NOW) == 0
    assert "ess_max_P_charge_dp_id=50000 W (const)" in caplog.text
    assert "ess_max_P_discharge_dp_id=30000 W (const)" in caplog.text
    assert "ess_max_E_dp_id=200000 Wh (const)" in caplog.text
    assert "pccImportLimitW=170000 W (const)" in caplog.text


def test_datapoint_sources_are_logged(cfg, monkeypatch, caplog):
    srv = FakeServer()
    srv.install(monkeypatch)
    assert main.run(cfg(), now=NOW) == 0
    assert "ess_max_P_charge_dp_id=20000 W (dp pmax)" in caplog.text
    assert "ess_max_E_dp_id=100000 Wh (dp cap)" in caplog.text


def test_clientID_legacy_alias_is_accepted(cfg, monkeypatch):
    srv = FakeServer()
    srv.install(monkeypatch)
    assert main.run(cfg(drop=("clientId",), clientID="legacy"), now=NOW) == 0


def test_failed_login_exits_1_without_data_requests(cfg, monkeypatch, caplog):
    srv = FakeServer()
    srv.install(monkeypatch, token=None)
    monkeypatch.setattr(query_utils, "get_datapoint", lambda ident: pytest.fail("data request after failed login"))
    assert main.run(cfg(), now=NOW) == 1
    assert "could not obtain an API token" in caplog.text


@pytest.mark.parametrize("change,needle", [
    (lambda s: s.datapoints.pop("pmax"), "does not exist"),
    (lambda s: s.datapoints["cap"].update(lastReadingValue=None), "has no value"),
    (lambda s: s.datapoints["charge"].update(lastReadingTime=iso(NOW - timedelta(hours=2))), "s old"),
])
def test_input_errors_stop_without_posting(cfg, monkeypatch, caplog, change, needle):
    srv = FakeServer()
    change(srv)
    srv.install(monkeypatch)
    assert main.run(cfg(), now=NOW) == 1
    assert srv.posted == [] and needle in caplog.text


@pytest.mark.parametrize("over,needle", [
    (dict(essChargeEfficiency=1.5), "essChargeEfficiency"),
    (dict(ess_max_P_charge_dp_id=True), "must be a datapoint identifier"),
])
def test_configuration_errors_stop(cfg, monkeypatch, caplog, over, needle):
    FakeServer().install(monkeypatch)
    assert main.run(cfg(**over), now=NOW) == 1
    assert needle in caplog.text


OPERATIONAL = ["pccImportLimitW", "pccExportLimitW",
               "ess_max_P_charge_dp_id", "ess_max_P_discharge_dp_id", "ess_max_E_dp_id"]


@pytest.mark.parametrize("param", OPERATIONAL)
def test_operational_params_are_required_without_defaults(cfg, monkeypatch, caplog, param):
    FakeServer().install(monkeypatch)
    assert main.run(cfg(drop=(param,)), now=NOW) == 1
    assert f"missing params: {param}" in caplog.text


# OPD AC1(a): datapoint form and number form give the same plan for the same value.
VALUES = {"pccImportLimitW": 14000, "pccExportLimitW": -5000,
          "ess_max_P_charge_dp_id": 8000, "ess_max_P_discharge_dp_id": 6000, "ess_max_E_dp_id": 80000}


def run_plan(cfg, monkeypatch, over, dps=()):
    srv = FakeServer()
    for name, value in dps:
        srv.datapoints[name] = {"lastReadingValue": value}
    srv.install(monkeypatch)
    assert main.run(cfg(**over), now=NOW) == 0
    return srv.posted[0][2]


@pytest.mark.parametrize("param", OPERATIONAL)
def test_each_operational_param_gives_the_same_plan_as_datapoint_and_as_number(cfg, monkeypatch, caplog, param):
    as_number = run_plan(cfg, monkeypatch, {param: VALUES[param]})
    assert f"{param}={VALUES[param]:g}" in caplog.text and f"{VALUES[param]:g} " in caplog.text
    caplog.clear()
    as_datapoint = run_plan(cfg, monkeypatch, {param: "dp_x"}, dps=[("dp_x", float(VALUES[param]))])
    assert f"(dp dp_x)" in caplog.text
    assert as_datapoint == as_number


def test_all_operational_params_as_datapoints_equal_all_as_numbers(cfg, monkeypatch):
    nums = run_plan(cfg, monkeypatch, dict(VALUES))
    dps = run_plan(cfg, monkeypatch, {k: f"dp_{k}" for k in VALUES}, dps=[(f"dp_{k}", float(v)) for k, v in VALUES.items()])
    assert dps == nums


@pytest.mark.parametrize("param", OPERATIONAL)
@pytest.mark.parametrize("kind", ["missing", "no_value"])
def test_missing_or_valueless_operational_datapoint_stops_without_posting(cfg, monkeypatch, caplog, param, kind):
    srv = FakeServer()
    if kind == "no_value":
        srv.datapoints["dp_x"] = {"lastReadingValue": None}
    srv.install(monkeypatch)
    assert main.run(cfg(**{param: "dp_x"}), now=NOW) == 1
    assert srv.posted == []
    msg = [r.getMessage() for r in caplog.records if "input error" in r.getMessage()]
    assert msg and param in msg[0] and "dp_x" in msg[0]
    assert ("does not exist" if kind == "missing" else "has no value") in msg[0]


# OPD AC1(c): R2 validation. Number form = configuration error, datapoint form = input error; both exit 1.
BAD = [
    ("ess_max_P_charge_dp_id", 0), ("ess_max_P_charge_dp_id", -5), ("ess_max_P_discharge_dp_id", 0),
    ("ess_max_P_discharge_dp_id", -5), ("ess_max_E_dp_id", 0), ("ess_max_E_dp_id", -1),
    ("pccExportLimitW", 170001), ("pccImportLimitW", -1),
]


@pytest.mark.parametrize("param,value", BAD)
def test_invalid_number_is_a_configuration_error(cfg, monkeypatch, caplog, param, value):
    srv = FakeServer()
    srv.install(monkeypatch)
    assert main.run(cfg(**{param: value}), now=NOW) == 1
    assert srv.posted == [] and "configuration error" in caplog.text and param in caplog.text


@pytest.mark.parametrize("param,value", BAD)
def test_invalid_datapoint_value_is_an_input_error(cfg, monkeypatch, caplog, param, value):
    srv = FakeServer()
    srv.datapoints["dp_x"] = {"lastReadingValue": float(value)}
    srv.install(monkeypatch)
    assert main.run(cfg(**{param: "dp_x"}), now=NOW) == 1
    assert srv.posted == []
    msg = [r.getMessage() for r in caplog.records if "input error" in r.getMessage()]
    assert msg and param in msg[0]


def test_export_limit_above_import_limit_via_two_datapoints_is_an_input_error(cfg, monkeypatch, caplog):
    srv = FakeServer()
    srv.datapoints["lo"] = {"lastReadingValue": 20000.0}
    srv.datapoints["hi"] = {"lastReadingValue": 15000.0}
    srv.install(monkeypatch)
    assert main.run(cfg(pccExportLimitW="lo", pccImportLimitW="hi"), now=NOW) == 1
    assert srv.posted == [] and "need pccExportLimitW <= pccImportLimitW" in caplog.text


def test_positive_lower_pcc_limit_is_accepted(cfg, monkeypatch):
    plan = run_plan(cfg, monkeypatch, {"pccExportLimitW": 5000})
    assert len(plan) > 0


# OPD AC1(d) end to end: charge 50 kW, discharge 30 kW.
def test_asymmetric_limits_bound_the_posted_plan(cfg, monkeypatch):
    def plan(cons, prod, stored_wh):
        srv = FakeServer()
        srv.datapoints["end"] = {"lastReadingValue": 80000.0}      # end target 80 kWh so that charging is wanted
        srv.forecasts["cons"] = series(10, cons)
        srv.forecasts["prod"] = series(10, prod)
        srv.datapoints["charge"]["lastReadingValue"] = stored_wh
        srv.install(monkeypatch)
        assert main.run(cfg(ess_max_P_charge_dp_id=50000, ess_max_P_discharge_dp_id=30000, pccImportLimitW=1000000,
                            pccExportLimitW=-1000000, essEffectiveEndCharge="end"), now=NOW) == 0
        return [r["value"] for r in srv.posted[0][2]]
    # Full battery, 100 kW load for 1.5 h, then a long surplus: it empties at the discharge limit.
    d = plan(lambda i: 100000.0 if i < 120 else 0.0, lambda i: -200000.0 if 120 <= i < 240 else 0.0, 80000.0)
    # Nearly empty battery and one 400 kW surplus step: it charges at the charge limit.
    c = plan(lambda i: 0.0, lambda i: -400000.0 if 120 <= i < 135 else 0.0, 15000.0)
    for values in (c, d):
        assert max(values) <= 50000 + 1e-3 and min(values) >= -30000 - 1e-3
    assert min(d) == pytest.approx(-30000, abs=1) and max(c) == pytest.approx(50000, abs=1)


# OPD AC1(f): removed / replaced params are configuration errors naming the replacement.
@pytest.mark.parametrize("param,value,needle", [
    ("essMinSafeLim", 0, "essMinSafeLim: removed"),
    ("npPriceIdentifier", "price", "npPriceIdentifier: removed"),
    ("ess_max_P_dp_id", "pmax", "ess_max_P_charge_dp_id and ess_max_P_discharge_dp_id"),
    ("essPowerLimitW", 50000, "ess_max_P_charge_dp_id and ess_max_P_discharge_dp_id"),
    ("essMaxCharge", 200000, "use ess_max_E_dp_id"),
])
def test_removed_or_replaced_param_is_a_configuration_error(cfg, monkeypatch, caplog, param, value, needle):
    srv = FakeServer()
    srv.install(monkeypatch)
    assert main.run(cfg(**{param: value}), now=NOW) == 1
    assert srv.posted == [] and "configuration error" in caplog.text and needle in caplog.text


def previous_plan():
    return [{"time": iso(START + timedelta(minutes=15 * i - 30)), "value": 1000.0 * i} for i in range(10)]


def test_not_enough_forecast_uses_the_previous_plan(cfg, monkeypatch, caplog):
    srv = FakeServer(hours=2)
    srv.forecasts["ess_p_a4_test"] = previous_plan()
    srv.install(monkeypatch)
    assert main.run(cfg(), now=NOW) == 0
    _, _, readings = srv.posted[0]
    assert readings[0]["time"] == "2026-11-14T10:00:00Z" and len(readings) == 8
    assert "fallback plan" in caplog.text


def test_failed_solve_uses_the_previous_plan(cfg, monkeypatch, caplog):
    srv = FakeServer()
    srv.forecasts["ess_p_a4_test"] = previous_plan()
    srv.install(monkeypatch)
    monkeypatch.setattr(main.a4model, "solve", lambda m, time_limit_s=300: a4model.PlanResult(ok=False, status="x/y"))
    assert main.run(cfg(writeDebugFile="never"), now=NOW) == 0
    assert len(srv.posted) == 1 and "solver result x/y" in caplog.text


def test_no_fallback_available_exits_1(cfg, monkeypatch, caplog):
    srv = FakeServer(hours=2)
    srv.install(monkeypatch)
    assert main.run(cfg(), now=NOW) == 1
    assert srv.posted == [] and "no previous plan" in caplog.text


def test_negative_ramp_weight_is_a_configuration_error(cfg, monkeypatch, caplog):
    FakeServer().install(monkeypatch)
    assert main.run(cfg(essRampWeight=-0.1), now=NOW) == 1
    assert "essRampWeight" in caplog.text


def test_ramp_weight_defaults_to_0_001(cfg, monkeypatch):
    srv = FakeServer()
    srv.install(monkeypatch)
    seen = []
    real_build = a4model.build_model
    monkeypatch.setattr(main.a4model, "build_model", lambda inp: seen.append(inp.ramp_weight) or real_build(inp))
    assert main.run(cfg(), now=NOW) == 0
    assert seen == [0.001]
