"""A4-IMP AC1(b)-(e): self-consumption model (needs glpsol)."""
import pytest

import model as a4model


def inputs(**over):
    # Hourly steps: load 10 kW; 30 kW PV in steps 2-4 (20 kW surplus x 3 h = 60 kWh).
    base = dict(interval_s=3600, load_kw=[10.0] * 8, pv_kw=[0, 0, -30, -30, -30, 0, 0, 0],
                p_max_kw=40.0, capacity_kwh=100.0, e0_kwh=30.0, e_end_kwh=30.0, soc_min_pct=15, soc_max_pct=80,
                import_limit_kw=1000.0, export_limit_kw=1000.0, eta_charge=0.95, eta_discharge=0.95,
                degradation_weight=1e-6, pcc_penalty_per_kwh=1000.0, end_penalty_per_kwh=0.0)
    base.update(over)
    return a4model.PlanInputs(**base)


def solve(inp):
    r = a4model.solve(a4model.build_model(inp), time_limit_s=60)
    assert r.ok, r.status
    return r


def test_pv_surplus_is_stored_by_making_room_first():
    # Without action E would go 30 -> 87 (> 80 max) and 7.4 kWh would be exported. The optimum discharges into the
    # morning load first (down to >= 15 kWh) so that all 60 kWh of surplus fit: export 0.
    r = solve(inputs())
    assert r.export_kwh == pytest.approx(0.0, abs=1e-6)
    assert all(15.0 - 1e-6 <= e <= 80.0 + 1e-6 for e in r.energy_kwh[1:])
    assert all(p >= -1e-6 for p in r.ess_kw[2:5])          # charges during the surplus
    assert sum(r.ess_kw[2:5]) == pytest.approx(60.0, abs=1e-4)


def test_efficiency_is_applied_when_storing():
    r = solve(inputs())
    stored = r.energy_kwh[5] - r.energy_kwh[2]
    assert stored == pytest.approx(60.0 * 0.95, abs=1e-4)


def test_full_battery_means_export_not_burning():
    # A4-A1 (h): battery at Emax, no load, export allowed: the 20 kWh of PV is exported, not burned by cycling.
    r = solve(inputs(e0_kwh=80.0, e_end_kwh=80.0, load_kw=[0.0] * 8, pv_kw=[0, 0, -20, 0, 0, 0, 0, 0]))
    assert r.export_kwh == pytest.approx(20.0, abs=1e-4)
    assert r.loss_kwh == pytest.approx(0.0, abs=1e-6)


def test_no_surplus_means_idle():
    # A4-A1 (g), the NRG case of 2026-11-14 at 60 s steps: PV never exceeds the load, so nothing can be gained and the
    # plan must be idle (it used to discharge to Emin and recharge from the grid).
    n = 240
    r = solve(inputs(interval_s=60, load_kw=[60.0] * n, pv_kw=[-10.0] * n, p_max_kw=200.0, capacity_kwh=438.0,
                     e0_kwh=191.9, e_end_kwh=191.9, import_limit_kw=170.0, export_limit_kw=0.0,
                     end_penalty_per_kwh=10.0))
    assert all(abs(p) < 1e-6 for p in r.ess_kw)
    assert r.loss_kwh == pytest.approx(0.0, abs=1e-6)


def test_pv_surplus_is_still_stored_with_losses_counted():
    r = solve(inputs())
    assert r.export_kwh == pytest.approx(0.0, abs=1e-6)
    assert r.loss_kwh > 0


def test_soft_pcc_limit_reports_overrun():
    r = solve(inputs(load_kw=[300.0] * 8, pv_kw=[0.0] * 8, import_limit_kw=100.0, p_max_kw=20.0))
    assert r.import_overrun_kwh > 100.0


def test_end_energy_option_is_honoured():
    r = solve(inputs(load_kw=[0.0] * 8, pv_kw=[0.0] * 8, e0_kwh=30.0, e_end_kwh=60.0, end_penalty_per_kwh=10.0))
    assert r.energy_kwh[-1] == pytest.approx(60.0, abs=1e-4)
    assert sum(max(p, 0) for p in r.ess_kw) == pytest.approx(30.0 / 0.95, abs=1e-4)


# --- A4-AF anti-flicker (AC1 a, b) ---------------------------------------------------------------------------------

def flicker_inputs(ramp):
    """60 s steps, no PV surplus, end target above the start: charging has equal cost in every step (F2)."""
    import math
    n = 90
    load = [90.0 + 3.0 * math.sin(1.7 * i) for i in range(n)]
    return inputs(interval_s=60, load_kw=load, pv_kw=[0.0] * n, p_max_kw=200.0, capacity_kwh=100.0,
                  e0_kwh=50.0, e_end_kwh=69.0, import_limit_kw=170.0, export_limit_kw=0.0,
                  end_penalty_per_kwh=10.0, ramp_weight=ramp)


def jumps(r, threshold_kw=1.0):
    return sum(1 for a, b in zip(r.ess_kw, r.ess_kw[1:]) if abs(b - a) > threshold_kw)


def test_fixture_reproduces_the_flicker_without_ramp_weight():
    r = solve(flicker_inputs(0.0))
    assert abs(r.energy_kwh[-1] - 69.0) < 1e-3
    assert jumps(r) > 2, r.ess_kw


def test_ramp_weight_gives_a_flat_plan():
    r = solve(flicker_inputs(0.001))
    assert abs(r.energy_kwh[-1] - 69.0) < 1e-3
    assert jumps(r) <= 2, r.ess_kw


def test_ramp_weight_has_small_impact():
    off = solve(flicker_inputs(0.0))
    on = solve(flicker_inputs(0.001))
    assert off.ramp_objective == 0
    assert (on.objective - on.ramp_objective) - off.objective <= 0.01
