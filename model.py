"""A4 optimisation model (A4-IMP R2-R4): ESS schedule that maximises PV self-consumption (minimises PCC export).

Units: power kW, energy kWh. Positive ESS power = charging. Net PCC power (import positive) = load + pv + ESS
(pv negative = generation), kept in a soft signed window [L, U] (OPD R4); separate charge/discharge limits (OPD R3).
The objective is in kWh-weighted units (no price): export + weighted conversion losses + tiny charge term + penalties.
"""
from dataclasses import dataclass, field
from typing import List, Optional

import pyomo.environ as pyo
from pyomo.opt import SolverStatus, TerminationCondition


@dataclass
class PlanInputs:
    interval_s: int
    load_kw: List[float]
    pv_kw: List[float]                  # negative = generation
    ch_max_kw: float                    # charge power limit, positive magnitude (OPD R3)
    dis_max_kw: float                   # discharge power limit, positive magnitude
    capacity_kwh: float
    e0_kwh: float                       # total stored energy at the start
    e_end_kwh: float                    # target stored energy at the end
    soc_min_frac: float               # SoC limits are fractions 0..1 (SFU R2)
    soc_max_frac: float
    import_limit_kw: float              # U: soft upper limit of the net PCC power (import positive)
    pcc_min_kw: float                   # L: soft lower limit of the net PCC power (signed; < 0 allows export, > 0 = minimum import)
    eta_charge: float = 0.95
    eta_discharge: float = 0.95
    degradation_weight: float = 1e-6    # per kWh charged
    pcc_penalty_per_kwh: float = 1000.0
    end_penalty_per_kwh: float = 10.0
    loss_weight: float = 2.0            # per kWh of conversion losses (A4-A1: stops needless cycling)
    ramp_weight: float = 0.001          # per kW of ESS power change between steps (A4-AF: stops flicker)


@dataclass
class PlanResult:
    ok: bool
    status: str
    ess_kw: List[float] = field(default_factory=list)
    energy_kwh: List[float] = field(default_factory=list)
    export_kwh: float = 0.0
    loss_kwh: float = 0.0
    import_overrun_kwh: float = 0.0
    below_limit_kwh: float = 0.0        # energy by which the net PCC power is below its lower limit
    end_deviation_kwh: float = 0.0
    objective: Optional[float] = None
    ramp_objective: float = 0.0


def usable_to_total_kwh(usable_kwh, capacity_kwh, soc_min_frac, soc_max_frac):
    """SFU R3: usable energy above the minimum SoC -> total stored energy.

    Returns (total_kwh, clamped_usable_kwh). total = capacity*min + clamp(usable, 0, capacity*(max-min))."""
    clamped = min(max(usable_kwh, 0.0), capacity_kwh * (soc_max_frac - soc_min_frac))
    return capacity_kwh * soc_min_frac + clamped, clamped


def energy_bounds(inp: PlanInputs):
    """[Emin, Emax] from the SoC window, widened to include E[0] if it is already outside."""
    e_min = inp.capacity_kwh * inp.soc_min_frac
    e_max = inp.capacity_kwh * inp.soc_max_frac
    return min(e_min, inp.e0_kwh), max(e_max, inp.e0_kwh)


def loss_kw(m, t):
    """Conversion losses in step t, kW: (1 - eta_ch) of the charge power and (1/eta_dis - 1) of the discharge power."""
    return m.ch[t] * (1 - m.eta_charge) + m.dis[t] * (1 / m.eta_discharge - 1)


def build_model(inp: PlanInputs) -> pyo.ConcreteModel:
    n = len(inp.load_kw)
    if n == 0 or len(inp.pv_kw) != n:
        raise ValueError("load and pv series must be non-empty and of equal length")
    dt_h = inp.interval_s / 3600.0
    e_min, e_max = energy_bounds(inp)
    e_end = min(max(inp.e_end_kwh, e_min), e_max)
    ch_max, dis_max = inp.ch_max_kw, inp.dis_max_kw

    m = pyo.ConcreteModel(name="A4")
    m.dt_h = dt_h
    m.eta_charge = inp.eta_charge
    m.eta_discharge = inp.eta_discharge
    m.T = pyo.RangeSet(0, n - 1)
    m.K = pyo.RangeSet(0, n)

    m.ch = pyo.Var(m.T, bounds=(0, ch_max))
    m.dis = pyo.Var(m.T, bounds=(0, dis_max))
    m.zc = pyo.Var(m.T, domain=pyo.Binary)
    m.ch_on = pyo.Constraint(m.T, rule=lambda m, t: m.ch[t] <= ch_max * m.zc[t])
    m.dis_on = pyo.Constraint(m.T, rule=lambda m, t: m.dis[t] <= dis_max * (1 - m.zc[t]))

    m.imp = pyo.Var(m.T, domain=pyo.NonNegativeReals)
    m.exp = pyo.Var(m.T, domain=pyo.NonNegativeReals)
    m.o_imp = pyo.Var(m.T, domain=pyo.NonNegativeReals)
    m.o_exp = pyo.Var(m.T, domain=pyo.NonNegativeReals)    # slack below the lower limit L (OPD R4)
    m.balance = pyo.Constraint(m.T, rule=lambda m, t:
                               m.imp[t] - m.exp[t] == inp.load_kw[t] + inp.pv_kw[t] + m.ch[t] - m.dis[t])
    # Signed net PCC window (OPD R4): net = imp - exp in [L - o_exp, U + o_imp]
    m.imp_limit = pyo.Constraint(m.T, rule=lambda m, t: m.imp[t] - m.exp[t] <= inp.import_limit_kw + m.o_imp[t])
    m.exp_limit = pyo.Constraint(m.T, rule=lambda m, t: m.imp[t] - m.exp[t] >= inp.pcc_min_kw - m.o_exp[t])

    m.E = pyo.Var(m.K)
    m.E[0].fix(inp.e0_kwh)
    for k in range(1, n + 1):
        m.E[k].setlb(e_min)
        m.E[k].setub(e_max)
    m.energy = pyo.Constraint(m.T, rule=lambda m, t:
                              m.E[t + 1] == m.E[t] + (m.ch[t] * inp.eta_charge - m.dis[t] / inp.eta_discharge) * dt_h)

    m.dev_up = pyo.Var(domain=pyo.NonNegativeReals)
    m.dev_down = pyo.Var(domain=pyo.NonNegativeReals)
    m.end_target = pyo.Constraint(expr=m.E[n] - e_end == m.dev_up - m.dev_down)

    # Ramp of the ESS power between consecutive steps (A4-AF R1): breaks ties between equal-cost timings
    m.TR = pyo.Set(initialize=list(range(1, n)))
    m.ramp = pyo.Var(m.TR, domain=pyo.NonNegativeReals)
    m.ramp_up = pyo.Constraint(m.TR, rule=lambda m, t:
                               m.ramp[t] >= (m.ch[t] - m.dis[t]) - (m.ch[t - 1] - m.dis[t - 1]))
    m.ramp_down = pyo.Constraint(m.TR, rule=lambda m, t:
                                 m.ramp[t] >= (m.ch[t - 1] - m.dis[t - 1]) - (m.ch[t] - m.dis[t]))
    m.ramp_term = pyo.Expression(expr=sum(m.ramp[t] for t in m.TR) * inp.ramp_weight)

    def objective(m):
        steps = sum(dt_h * (m.exp[t] + m.ch[t] * inp.degradation_weight
                            + loss_kw(m, t) * inp.loss_weight
                            + (m.o_imp[t] + m.o_exp[t]) * inp.pcc_penalty_per_kwh) for t in m.T)
        # A4-AF R2: ramp term per kW of change, not scaled by dt
        return steps + (m.dev_up + m.dev_down) * inp.end_penalty_per_kwh + m.ramp_term
    m.cost = pyo.Objective(rule=objective, sense=pyo.minimize)
    return m


def solve(m: pyo.ConcreteModel, time_limit_s: float = 300, solver_name: str = "glpk") -> PlanResult:
    solver = pyo.SolverFactory(solver_name)
    if solver_name == "glpk":
        solver.options["tmlim"] = int(time_limit_s)
    results = solver.solve(m, load_solutions=False)
    term = results.solver.termination_condition
    status = f"{results.solver.status}/{term}"
    usable = term in (TerminationCondition.optimal, TerminationCondition.feasible) or (
        term == TerminationCondition.maxTimeLimit and len(results.solution) > 0)
    if results.solver.status not in (SolverStatus.ok, SolverStatus.warning) or not usable:
        return PlanResult(ok=False, status=status)
    m.solutions.load_from(results)
    return PlanResult(
        ok=True,
        status=status,
        ess_kw=[pyo.value(m.ch[t]) - pyo.value(m.dis[t]) + 0.0 for t in m.T],
        energy_kwh=[pyo.value(m.E[k]) for k in m.K],
        export_kwh=sum(pyo.value(m.exp[t]) for t in m.T) * m.dt_h,
        loss_kwh=sum(pyo.value(loss_kw(m, t)) for t in m.T) * m.dt_h,
        import_overrun_kwh=sum(pyo.value(m.o_imp[t]) for t in m.T) * m.dt_h,
        below_limit_kwh=sum(pyo.value(m.o_exp[t]) for t in m.T) * m.dt_h,
        end_deviation_kwh=pyo.value(m.dev_up) - pyo.value(m.dev_down),
        objective=pyo.value(m.cost),
        ramp_objective=pyo.value(m.ramp_term),
    )
