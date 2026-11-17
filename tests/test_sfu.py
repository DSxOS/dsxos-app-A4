"""SFU AC1(a)-(d): SoC limits as fractions, usable energy inputs converted to total energy."""
import logging

import pytest

import main
import model as a4model
from test_main import FakeServer, NOW, cfg  # noqa: F401  (cfg is a fixture)
from test_model import inputs, solve

CAP, SMIN, SMAX = 350.0, 0.15, 0.80


# AC1(a): validation
def test_percent_limits_are_rejected_with_a_fractions_hint(cfg, monkeypatch, caplog):
    srv = FakeServer()
    srv.install(monkeypatch)
    assert main.run(cfg(ess_min_soc=15, ess_max_soc=80), now=NOW) == 1
    assert srv.posted == []
    assert "configuration error" in caplog.text
    assert "fractions" in caplog.text and "ess_min_soc=15" in caplog.text and "ess_max_soc=80" in caplog.text
    assert "use 0.15" in caplog.text


def test_percent_limits_from_datapoints_are_an_input_error_with_the_hint(cfg, monkeypatch, caplog):
    srv = FakeServer()
    srv.datapoints.update(lo={"lastReadingValue": 15.0}, hi={"lastReadingValue": 80.0})
    srv.install(monkeypatch)
    assert main.run(cfg(ess_min_soc="lo", ess_max_soc="hi"), now=NOW) == 1
    assert srv.posted == [] and "input error" in caplog.text and "fractions" in caplog.text


def test_fraction_limits_are_accepted():
    assert main.limit_problems({"ess_min_soc": 0.15, "ess_max_soc": 0.80}) == []
    assert main.limit_problems({"ess_min_soc": 0.0, "ess_max_soc": 1.0}) == []


@pytest.mark.parametrize("mn,mx", [(0.8, 0.15), (-0.1, 0.5), (0.5, 0.5), (0.2, 1.01)])
def test_invalid_fraction_limits_are_rejected(mn, mx):
    problems = main.limit_problems({"ess_min_soc": mn, "ess_max_soc": mx})
    assert problems and all("ess_min_soc" in m and "ess_max_soc" in m for _, m in problems)


def test_valid_fraction_limits_run_end_to_end(cfg, monkeypatch):
    srv = FakeServer()
    srv.install(monkeypatch)
    assert main.run(cfg(ess_min_soc=0.15, ess_max_soc=0.80), now=NOW) == 0
    assert len(srv.posted) == 1


# AC1(b): bounds
def test_energy_bounds_are_capacity_times_fraction():
    inp = inputs(capacity_kwh=CAP, soc_min_frac=SMIN, soc_max_frac=SMAX, e0_kwh=100.0, e_end_kwh=100.0)
    assert a4model.energy_bounds(inp) == pytest.approx((52.5, 280.0))


# AC1(c): usable -> total
@pytest.mark.parametrize("usable,total,clamped", [(34.3, 86.8, 34.3), (-5.0, 52.5, 0.0), (300.0, 280.0, 227.5)])
def test_usable_to_total(usable, total, clamped):
    t, c = a4model.usable_to_total_kwh(usable, CAP, SMIN, SMAX)
    assert t == pytest.approx(total, abs=0.01) and c == pytest.approx(clamped, abs=0.01)


@pytest.mark.parametrize("usable_wh,total", [(34300.0, 86.8), (-5000.0, 52.5), (300000.0, 280.0)])
def test_to_total_warns_only_when_clamped(caplog, usable_wh, total):
    with caplog.at_level(logging.WARNING, logger="A4Runner"):
        got = main.to_total("essEffectiveCharge", usable_wh / 1000.0, CAP, SMIN, SMAX)
    assert got == pytest.approx(total, abs=0.01)
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert bool(warned) == (usable_wh != 34300.0)
    if warned:
        assert f"{usable_wh / 1000.0:.3f}" in warned[0].getMessage() and "clamped" in warned[0].getMessage()


def sfu_server(srv, charge_wh, end_wh=None):
    srv.datapoints["cap"]["lastReadingValue"] = CAP * 1000.0
    srv.datapoints["charge"]["lastReadingValue"] = charge_wh
    if end_wh is not None:
        srv.datapoints["end"] = {"lastReadingValue": end_wh}


def resolved_line(caplog):
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("resolved inputs")]
    assert len(lines) == 1                                                       # R4: one INFO line
    return lines[0]


@pytest.mark.parametrize("end_wh,total_end", [(34300.0, 86.8), (-5000.0, 52.5), (300000.0, 280.0)])
def test_end_target_is_converted_like_the_start_energy(cfg, monkeypatch, caplog, end_wh, total_end):
    srv = FakeServer()
    sfu_server(srv, 34300.0, end_wh)
    srv.install(monkeypatch)
    caplog.set_level(logging.INFO)
    assert main.run(cfg(essEffectiveEndCharge="end"), now=NOW) == 0
    line = resolved_line(caplog)
    assert f"total end target={total_end:.3f} kWh" in line and "total start energy=86.800 kWh" in line
    assert "ess_min_soc=0.15, ess_max_soc=0.8" in line and "capacity=350.000 kWh" in line
    assert "usable start energy=34.300 kWh" in line and "charge limit=20 kW" in line
    assert "PCC limits=0..170 kW" in line


def test_default_end_target_is_the_converted_start_energy(cfg, monkeypatch, caplog):
    srv = FakeServer()
    sfu_server(srv, 34300.0)
    srv.install(monkeypatch)
    caplog.set_level(logging.INFO)
    assert main.run(cfg(), now=NOW) == 0
    assert "total end target=86.800 kWh" in resolved_line(caplog)


# AC1(d): a full solve keeps E[k] within the window
@pytest.mark.parametrize("usable", [34.3, -5.0, 300.0])
def test_full_solve_keeps_energy_within_the_window(usable):
    total, _ = a4model.usable_to_total_kwh(usable, CAP, SMIN, SMAX)
    # Surplus that would push the energy far outside the window without the bounds.
    inp = inputs(capacity_kwh=CAP, soc_min_frac=SMIN, soc_max_frac=SMAX, e0_kwh=total, e_end_kwh=total,
                 ch_max_kw=100.0, dis_max_kw=100.0, load_kw=[10.0] * 8, pv_kw=[0, 0, -150, -150, -150, 0, 0, 0],
                 end_penalty_per_kwh=10.0)
    r = solve(inp)
    assert all(52.5 - 1e-6 <= e <= 280.0 + 1e-6 for e in r.energy_kwh)
