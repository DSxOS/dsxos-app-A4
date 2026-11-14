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
        "ess_max_P_dp_id": "pmax", "ess_max_E_dp_id": "cap", "essEffectiveCharge": "charge",
        "npPriceIdentifier": "unused", "essMinSafeLim": "unused",
        "ess_min_soc": 15, "ess_max_soc": 80, "pccImportLimitW": 170000, "pccExportLimitW": 0,
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
    path = cfg(drop=("ess_max_P_dp_id", "ess_max_E_dp_id"), essPowerLimitW=50000, essMaxCharge=200000)
    assert main.run(path, now=NOW) == 0
    assert "Pmax=50.0 kW, capacity=200.0 kWh" in caplog.text


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
    (dict(ess_min_soc=90), "ess_min_soc"),
])
def test_configuration_errors_stop(cfg, monkeypatch, caplog, over, needle):
    FakeServer().install(monkeypatch)
    assert main.run(cfg(**over), now=NOW) == 1
    assert needle in caplog.text


def test_missing_limit_source_is_a_configuration_error(cfg, monkeypatch, caplog):
    FakeServer().install(monkeypatch)
    assert main.run(cfg(drop=("ess_max_P_dp_id",)), now=NOW) == 1
    assert "ess_max_P_dp_id or essPowerLimitW" in caplog.text


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
