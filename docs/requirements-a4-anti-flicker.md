# A4 anti-flicker: ramp penalty (A4-AF)

Status: **FROZEN 2026-11-15**. The owner asked in session ("proceed with a4") to carry over the A5 anti-flicker change
(dsxos-app-A5 A5-AF, merged b3250d8). Base: A4 main `3c0c3a9` (A4-IMP + A4-A1). Changes only as numbered amendments in §6.

## 1. Facts

- **F1.** A5 flickered on DSxOS-NRG on 2026-11-15: its plan switched between 0 and ≈93 kW from one minute to the next
  inside a constant-price slot. The cause was a degenerate optimum: equal-cost arrangements, from which GLPK picks an
  on/off vertex.
- **F2.** The A4 objective (A4-IMP R4 + A4-A1) has no time-varying coefficient: export + `essLossWeight`·losses +
  `essDegradationWeight`·charge + penalties. Energy moved without PV surplus forcing its timing therefore has equal
  cost in every step of the horizon. This covers charging towards the end target, and discharging to make room before
  a surplus. A4 is thus more exposed to flicker than A5.
- **F3.** The current NRG plans are idle (no PV surplus in November), so the flicker is not visible live now.

## 2. Owner decision

| # | Decision |
|---|---|
| 1 | Same approach as A5-AF: a small linear penalty on the change of ESS power between consecutive steps. Default weight 0.001 per kW of change, in A4's kWh-weighted objective units. |

## 3. Requirements

- **R1. Ramp variables.** For k = 1..N−1, `r[k] ≥ P[k] − P[k−1]` and `r[k] ≥ P[k−1] − P[k]`, with `r[k] ≥ 0` and
  `P[k] = ch[k] − dis[k]`. No new binaries. No ramp term for step 0.
- **R2. Objective.** The A4 objective plus `Σ_{k=1..N−1} r[k] · essRampWeight`. The term is **not** multiplied by Δt.
- **R3. Parameter.** `essRampWeight`, optional, default **0.001** (objective units per kW of change). `0` disables the
  term, and the model is then the A4-A1 model. A negative value is a configuration error.
- **R4. Reporting.** `PlanResult` reports the ramp term separately (`ramp_objective`). The `solved` log line shows it.
  The existing `objective` remains the full objective value.
- **R5. Docs.** The README parameter table and the objective description include the new term. `example_config.yaml`
  lists the parameter (commented, with its default, like the other optional weights).
- **R6. Unchanged.** All other A4 requirements stay as they are, and all existing tests pass unchanged.

## 4. Acceptance criteria

- **AC1.** `python -m pytest -q tests` passes (python:3.11-slim + glpk-utils), including new tests for:
  - (a) **Flat plan.** A fixture with 60 s steps, no PV surplus, and an end target above the start energy (charging
    has to happen somewhere in the horizon). With the default weight, the plan has at most 2 steps with |ΔP| > 1 kW.
    With `essRampWeight = 0`, the same fixture must produce more than 2 such steps, so the fixture does reproduce the
    flicker.
  - (b) **Small impact.** In (a), the objective without the ramp term is at most 0.01 higher with the default weight
    than with the weight off.
  - (c) **Config.** A negative `essRampWeight` is a configuration error. If the parameter is absent, the default 0.001
    is used.
- **AC2.** `python -m py_compile *.py` passes.
- **AC3 (live, DSxOS-NRG).** With the submodule at this change and the NRG `a4.yaml`, the first A4 run after the
  deploy:
  - logs `solved (ok/optimal)`, with a solve time of at most 60 s;
  - posts a plan with SoC within 15–80 %, recomputed from the posted plan;
  - posts a plan with at most 2 steps with |ΔP| > 5 kW in every 15-min window.

## 5. Out of scope

- A1: its objective is the total |correction|, which ties the timing to the PCC violations.
- Showing the effect live under PV surplus (not possible in November).

## 6. Amendments

None.
