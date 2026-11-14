"""A4 optimisation model (A4-IMP R2-R4): ESS schedule that maximises PV self-consumption (minimises PCC export).

Units: power kW, energy kWh. Positive ESS power = charging. PCC = load + pv + ESS (pv negative = generation).
The objective is in kWh-weighted units (no price): export + tiny charge term + penalties.
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
    p_max_kw: float
    capacity_kwh: float
    e0_kwh: float                       # total stored energy at the start
    e_end_kwh: float                    # target stored energy at the end
    soc_min_pct: float
    soc_max_pct: float
    import_limit_kw: float              # soft
    export_limit_kw: float              # soft, positive number
    eta_charge: float = 0.95
    eta_discharge: float = 0.95
    degradation_weight: float = 1e-6    # per kWh charged (tie-breaker against needless cycling)
    pcc_penalty_per_kwh: float = 1000.0
    end_penalty_per_kwh: float = 10.0


@dataclass
class PlanResult:
    ok: bool
    status: str
    ess_kw: List[float] = field(default_factory=list)
    energy_kwh: List[float] = field(default_factory=list)
    export_kwh: float = 0.0
    import_overrun_kwh: float = 0.0
    export_overrun_kwh: float = 0.0
    end_deviation_kwh: float = 0.0
    objective: Optional[float] = None


def energy_bounds(inp: PlanInputs):
    """[Emin, Emax] from the SoC window, widened to include E[0] if it is already outside."""
    e_min = inp.capacity_kwh * inp.soc_min_pct / 100.0
    e_max = inp.capacity_kwh * inp.soc_max_pct / 100.0
    return min(e_min, inp.e0_kwh), max(e_max, inp.e0_kwh)


def build_model(inp: PlanInputs) -> pyo.ConcreteModel:
    n = len(inp.load_kw)
    if n == 0 or len(inp.pv_kw) != n:
        raise ValueError("load and pv series must be non-empty and of equal length")
    dt_h = inp.interval_s / 3600.0
    e_min, e_max = energy_bounds(inp)
    e_end = min(max(inp.e_end_kwh, e_min), e_max)
    p_max = inp.p_max_kw

    m = pyo.ConcreteModel(name="A4")
    m.dt_h = dt_h
    m.T = pyo.RangeSet(0, n - 1)
    m.K = pyo.RangeSet(0, n)

    m.ch = pyo.Var(m.T, bounds=(0, p_max))
    m.dis = pyo.Var(m.T, bounds=(0, p_max))
    m.zc = pyo.Var(m.T, domain=pyo.Binary)
    m.ch_on = pyo.Constraint(m.T, rule=lambda m, t: m.ch[t] <= p_max * m.zc[t])
    m.dis_on = pyo.Constraint(m.T, rule=lambda m, t: m.dis[t] <= p_max * (1 - m.zc[t]))

    m.imp = pyo.Var(m.T, domain=pyo.NonNegativeReals)
    m.exp = pyo.Var(m.T, domain=pyo.NonNegativeReals)
    m.o_imp = pyo.Var(m.T, domain=pyo.NonNegativeReals)
    m.o_exp = pyo.Var(m.T, domain=pyo.NonNegativeReals)
    m.balance = pyo.Constraint(m.T, rule=lambda m, t:
                               m.imp[t] - m.exp[t] == inp.load_kw[t] + inp.pv_kw[t] + m.ch[t] - m.dis[t])
    m.imp_limit = pyo.Constraint(m.T, rule=lambda m, t: m.imp[t] <= inp.import_limit_kw + m.o_imp[t])
    m.exp_limit = pyo.Constraint(m.T, rule=lambda m, t: m.exp[t] <= inp.export_limit_kw + m.o_exp[t])

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

    def objective(m):
        steps = sum(dt_h * (m.exp[t] + m.ch[t] * inp.degradation_weight
                            + (m.o_imp[t] + m.o_exp[t]) * inp.pcc_penalty_per_kwh) for t in m.T)
        return steps + (m.dev_up + m.dev_down) * inp.end_penalty_per_kwh
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
        import_overrun_kwh=sum(pyo.value(m.o_imp[t]) for t in m.T) * m.dt_h,
        export_overrun_kwh=sum(pyo.value(m.o_exp[t]) for t in m.T) * m.dt_h,
        end_deviation_kwh=pyo.value(m.dev_up) - pyo.value(m.dev_down),
        objective=pyo.value(m.cost),
    )
