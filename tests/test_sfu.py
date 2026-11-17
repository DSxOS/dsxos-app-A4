"""SFU-A1 AC1'(a)-(d): usable capacity, SoC limits removed, energy inputs used directly as clamp(u, 0, capacity)."""
import logging

import pytest

import main
import model as a4model
from test_main import FakeServer, NOW, cfg  # noqa: F401  (cfg is a fixture)
from test_model import inputs, solve

CAP = 227.5


# AC1'(a): the SoC parameters are stale
@pytest.mark.parametrize("over", [dict(ess_min_soc=0.15), dict(ess_max_soc=0.80), dict(ess_min_soc=0.15, ess_max_soc=0.80)])
def test_soc_params_fail_with_the_stale_parameter_error(cfg, monkeypatch, caplog, over):
    srv = FakeServer()
    srv.install(monkeypatch)
    assert main.run(cfg(**over), now=NOW) == 1
    assert srv.posted == []
    assert "configuration error" in caplog.text and "removed or replaced params" in caplog.text
    for name in over:
        assert f"{name}: removed (SFU-A1)" in caplog.text


def test_soc_params_are_not_operational_params():
    assert "ess_min_soc" not in main.OPERATIONAL and "ess_max_soc" not in main.OPERATIONAL
    assert {"ess_min_soc", "ess_max_soc"} <= set(main.REMOVED)
    assert main.limit_problems({"ess_max_E_dp_id": CAP * 1000.0}) == []


def test_config_without_soc_params_runs(cfg, monkeypatch):
    srv = FakeServer()
    srv.install(monkeypatch)
    assert main.run(cfg(), now=NOW) == 0
    assert len(srv.posted) == 1


# AC1'(b): bounds
def test_energy_bounds_are_zero_and_capacity():
    inp = inputs(capacity_kwh=CAP, e0_kwh=100.0, e_end_kwh=100.0)
    assert a4model.energy_bounds(inp) == (0.0, CAP)


# AC1'(c): energy inputs are used directly
@pytest.mark.parametrize("u,used", [(34.3, 34.3), (-5.0, 0.0), (300.0, CAP)])
def test_clamp_energy_warns_only_when_clamped(caplog, u, used):
    with caplog.at_level(logging.WARNING, logger="A4Runner"):
        got = main.clamp_energy("essEffectiveCharge", u, CAP)
    assert got == pytest.approx(used)
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert bool(warned) == (u != used)
    if warned:
        msg = warned[0].getMessage()
        assert f"{u:.3f}" in msg and f"{used:.3f}" in msg and "clamped" in msg


def sfu_server(srv, charge_wh, end_wh=None):
    srv.datapoints["cap"]["lastReadingValue"] = CAP * 1000.0
    srv.datapoints["charge"]["lastReadingValue"] = charge_wh
    if end_wh is not None:
        srv.datapoints["end"] = {"lastReadingValue": end_wh}


def resolved_line(caplog):
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("resolved inputs")]
    assert len(lines) == 1                                                       # R4': one INFO line
    return lines[0]


@pytest.mark.parametrize("end_wh,used", [(34300.0, 34.3), (-5000.0, 0.0), (300000.0, CAP)])
def test_end_target_is_clamped_like_the_start_energy(cfg, monkeypatch, caplog, end_wh, used):
    srv = FakeServer()
    sfu_server(srv, 34300.0, end_wh)
    srv.install(monkeypatch)
    caplog.set_level(logging.INFO)
    assert main.run(cfg(essEffectiveEndCharge="end"), now=NOW) == 0
    line = resolved_line(caplog)
    assert "capacity=227.500 kWh" in line and "start energy=34.300 kWh" in line
    assert (f"end target={used:.3f} kWh" in line) if end_wh / 1000.0 == used else (f"{used:.3f} kWh used" in line)
    assert "charge limit=20 kW" in line and "PCC limits=0..170 kW" in line
    assert "soc" not in line.lower() and "fraction" not in line


def test_raw_and_used_start_energy_are_logged_when_clamped(cfg, monkeypatch, caplog):
    srv = FakeServer()
    sfu_server(srv, 300000.0)
    srv.install(monkeypatch)
    caplog.set_level(logging.INFO)
    assert main.run(cfg(), now=NOW) == 0
    assert "start energy=300.000 kWh raw, 227.500 kWh used" in resolved_line(caplog)
    assert any(r.levelno == logging.WARNING and "essEffectiveCharge" in r.getMessage() for r in caplog.records)


def test_default_end_target_is_the_clamped_start_energy(cfg, monkeypatch, caplog):
    srv = FakeServer()
    sfu_server(srv, 300000.0)
    srv.install(monkeypatch)
    caplog.set_level(logging.INFO)
    assert main.run(cfg(), now=NOW) == 0
    assert "end target = start (default)" in resolved_line(caplog)


# AC1'(d): a full solve keeps E[k] within [0, capacity]
@pytest.mark.parametrize("u", [34.3, -5.0, 300.0])
def test_full_solve_keeps_energy_within_zero_and_capacity(u):
    e0 = min(max(u, 0.0), CAP)
    # Surplus that would push the energy far above the capacity without the bounds, then a long deficit.
    inp = inputs(capacity_kwh=CAP, e0_kwh=e0, e_end_kwh=e0,
                 ch_max_kw=100.0, dis_max_kw=100.0, load_kw=[10.0] * 8, pv_kw=[0, 0, -150, -150, -150, 0, 0, 0],
                 end_penalty_per_kwh=10.0)
    r = solve(inp)
    assert all(-1e-6 <= e <= CAP + 1e-6 for e in r.energy_kwh)
