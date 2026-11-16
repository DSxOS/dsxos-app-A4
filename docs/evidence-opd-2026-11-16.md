# OPD: evidence, 2026-11-16 (A4)

Requirements: [requirements-opd-operational-datapoints.md](requirements-opd-operational-datapoints.md). Implementation 882bf2f.

| AC | Verdict |
|---|---|
| AC1 | PASS |
| AC2 | PASS |
| AC3 | pending (live on DSxOS-NRG after the site config and pin update) |

Independent validator (NRG VM `dsxos`, `git archive 882bf2f`): py_compile 0; pytest 105 passed, exit 0. AC1 (a)-(f) PASS, AC2 PASS. `tests/legacy_model.py` byte-identical to `996f54e:model.py` apart from its docstring. Spot check: 25 random equal-limit L≤0 cases vs legacy: 0 diffs (objective 1e-6, export 1e-4 kWh); asymmetric 50/30 with positive L stays in [−30, +48.2] kW and reports the overrun when infeasible.
