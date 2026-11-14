# A4-IMP evidence (2026-11-14)

Independent validator runs; commands and outputs abbreviated from the validator reports.

## AC1, AC2 — efc55b0 (first validation) and 632ef0c (after A4-A1)
- Host Taltech-ps-dt, throwaway `python:3.11-slim` (Python 3.11.18, pytest 8.3.5, GLPSOL 5.0), clone at the commit.
- 0bb6f76: `pytest -v` → `25 passed`, rc 0; `py_compile` rc 0.
- 632ef0c: `git rev-parse HEAD` → 632ef0cde569…; `pytest -v` → `27 passed in 0.52s` (incl. A4-A1 (g)
  `test_no_surplus_means_idle`, (h) `test_full_battery_means_export_not_burning`); `py_compile` → COMPILE_OK.
- AC2: `git grep -nE "^\s*(np|pd|value)\s*=[^=]|import \*|for (np|pd|value) in" 632ef0c -- '*.py'` → no match (rc 1).
- Cleanup: container, /tmp dir and image removed (`Deleted sha256:07d48240…`).

## AC3 — live DSxOS-NRG (site main 8ef2866, container code md5 = 632ef0c)
- First run 13:45Z (0bb6f76): exit 0, 1896 steps, SoC 15.28–43.81 %, export 0 — but needless cycling (118.7 kWh
  discharged, 131.6 kWh recharged) → amendment A4-A1.
- Run 14:00:10Z (632ef0c): `solved (ok/optimal): 1881 steps 2026-11-14T14:00:00Z .. 2026-11-15T21:20:00Z,
  export -0.00 kWh, losses 0.00 kWh, end energy 191.8 kWh`; `plan posted to 'ess_p_a4_test'`; no non-zero/ERROR lines;
  1.5 s.
- DB `ess_p_a4_test` prognosis 46604: 1881 readings, 60 s steps, aligned start, horizon 31.3 h, all values 0 W
  (no PV surplus in the horizon: PV ≤ 10 kW, load ≥ 53 kW), SoC 43.78 % throughout, PCC 53.5–113.7 kW.
- AC3 PASS. Caveat: the live data had no PV surplus, so charging is exercised only by the pytest model cases.
