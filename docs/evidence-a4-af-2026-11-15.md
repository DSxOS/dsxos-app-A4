# A4-AF: evidence, 2026-11-15

Requirements: [requirements-a4-anti-flicker.md](requirements-a4-anti-flicker.md). Commit `bd6a240`.

| AC | Verdict | Evidence |
|---|---|---|
| AC1 | PASS | Independent validator, DSxOS-NRG VM (`hostname` = `dsxos`), throwaway `python:3.11-slim` + `glpk-utils`, tree `git archive bd6a240`: `python -m pytest -q -rA tests -p no:cacheprovider` gives `32 passed in 1.43s`, `PYTEST_EXIT=0`. The five new tests pass; the 25 existing tests pass unchanged |
| AC2 | PASS | Same container: `python -m py_compile *.py`, `PYCOMPILE_EXIT=0` |
| AC3 | PASS | DSxOS-NRG, `a4` pinned to `6027dd6`, container created 2026-11-15 09:31:46Z. Independent validator, details below |

AC3, the run of 2026-11-15 09:45:10Z:

- **Code check:** `sudo docker exec a4 grep -c essRampWeight /app/main.py` returns `3`.
- **Log:** `09:45:13,588 solved (ok/optimal): 1776 steps 2026-11-15T09:45:00Z .. 2026-11-16T15:20:00Z, export 0.00 kWh, losses 0.00 kWh, ramp term 0.0000, end energy 241.0 kWh`.
  The run started at 09:45:10,679, so the solve took about 2.9 s.
- **DB (prognosis 47596 of `ess_p_a4_test`):** 1776 readings, all 60 s apart, all 0 kW.
- **SoC, recomputed from the plan:** 55.02 % throughout, within 15–80 %.
- **Jumps:** 0 steps with |ΔP| > 5 kW in any 15-min window, clock-aligned or sliding.
- **Limitation:** there is no PV surplus in November, so the plan is idle and this run shows only that the model is
  sane live, as §1 F3 and §5 of the requirements anticipate. The anti-flicker effect itself is shown by AC1(a).

Validator probe of the AC1(a) fixture (60 s steps, no PV, end target 69 kWh from 50 kWh):

| Weight | Jumps with \|ΔP\| > 1 kW | Plan | Objective (without the ramp term) |
|---|---|---|---|
| 0 | 28 | 0 kW with isolated 77–82 kW spikes (the flicker) | 2.00002 |
| 0.001 | 0 | flat 13.33 kW in all 90 steps | 2.00002 (difference ≈ 1e-13) |

The validator reviewed the diff `3c0c3a9..bd6a240`: R1–R6 are met, with no defects. Cleanup: the VM's /tmp files are removed, and no `a4af-*`
containers remain.
