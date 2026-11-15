# A4-AF: evidence, 2026-11-15

Requirements: [requirements-a4-anti-flicker.md](requirements-a4-anti-flicker.md). Commit `bd6a240`.

| AC | Verdict | Evidence |
|---|---|---|
| AC1 | PASS | Independent validator, DSxOS-NRG VM (`hostname` = `dsxos`), throwaway `python:3.11-slim` + `glpk-utils`, tree `git archive bd6a240`: `python -m pytest -q -rA tests -p no:cacheprovider` gives `32 passed in 1.43s`, `PYTEST_EXIT=0`. The five new tests pass; the 25 existing tests pass unchanged |
| AC2 | PASS | Same container: `python -m py_compile *.py`, `PYCOMPILE_EXIT=0` |
| AC3 | pending | Live on DSxOS-NRG after the submodule pin and deploy |

Validator probe of the AC1(a) fixture (60 s steps, no PV, end target 69 kWh from 50 kWh):

| Weight | Jumps with \|ΔP\| > 1 kW | Plan | Objective (without the ramp term) |
|---|---|---|---|
| 0 | 28 | 0 kW with isolated 77–82 kW spikes (the flicker) | 2.00002 |
| 0.001 | 0 | flat 13.33 kW in all 90 steps | 2.00002 (difference ≈ 1e-13) |

The validator reviewed the diff `3c0c3a9..bd6a240`: R1–R6 are met, with no defects. Cleanup: the VM's /tmp files are removed, and no `a4af-*`
containers remain.
