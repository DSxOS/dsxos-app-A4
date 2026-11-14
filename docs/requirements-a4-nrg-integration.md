# A4 fixes and NRG integration (A4-IMP)

Status: **FROZEN 2026-11-14**. The owner applied the A5 process and decisions ("same process and logic as A5") and
answered the A4-specific questions on 2026-11-14. Branch `feature/nrg-integration` off `main` `3501b73`. Changes only as
numbered amendments in §6.

The reference is dsxos-app-A5 `docs/requirements-a5-improvements.md` (A5-IMP) and
`docs/requirements-client-credentials-glpk.md` (A5-AUTH). Requirements marked "as A5" mean the same as there.

## 1. Facts (main `3501b73`)

- **F1.** A4 maximises PV self-consumption: it minimises the PCC export plus a tiny charge term (1e-6). It reads no
  price; `npPriceIdentifier` is configured but unused. The code calls itself "a proof of concept".
- **F2.** It shares the A5 helper code, so it has A5's audit findings:
  - forecasts capped at 500 values (no paging);
  - the start is not aligned;
  - one POST per value;
  - `essMinSafeLim` is read but unused;
  - `print`s, a debug file on every run, unpinned dependencies, no tests.
- **F3.** The login result is not checked. A failed token request gives `TypeError` (seen on NRG 2026-11-13 22:00Z, after
  `401 invalid_client`).
- **F4.** Model specifics:
  - ESS power and capacity are fixed numbers (`essPowerLimitW`, `essMaxCharge`).
  - SoC is `E/capacity` with bounds 0–100 %, so `ess_min_soc`/`ess_max_soc` are unused.
  - There is no efficiency.
  - The end-SoC equality is hard, and so are the PCC limits (with binaries). Either can make the problem infeasible.
  - On an empty result, `essPowerPrognosisRaw` is undefined, which gives `NameError`. There is no fallback.
- **F5.** The config contract uses `clientID` in `example_config.yaml`, but the code reads `clientId` (KeyError on NRG).

## 2. Owner decisions

| # | Decision |
|---|---|
| D1 | Apply A5-IMP and A5-AUTH: login check, paging, aligned start, batch output, input/config errors, fallback, configurable values, code split, tests, pinned dependencies, cleanup. |
| D2 | Inputs match NRG's current format, as for A5: the start energy is the **total** stored energy in Wh; the SoC window `ess_min_soc`/`ess_max_soc` is enforced; efficiencies are parameters, default 0.95/0.95. |
| D3 | Soft PCC limits with a penalty; the overrun is reported. |
| D4 | End target as in A5: soft, default end = start, optional `essEffectiveEndCharge`. |
| D5 | ESS power and capacity from datapoints (`ess_max_P_dp_id`, `ess_max_E_dp_id`, the same names as A5). If those are not set, the fixed `essPowerLimitW`/`essMaxCharge` are used. |
| D6 | NRG: interval 60 s; output to the test datapoint `ess_p_a4_test`; own API credential. |

## 3. Requirements

- **R1. Shared behaviour (as A5).**
  - **Authentication:** `clientId` + secret, as A5-AUTH R1–R2. A failure means ERROR and exit 1, before any data
    request. `clientID` is accepted as a legacy alias of `clientId`.
  - **Inputs and time grid:** paged forecasts, as A5-IMP R7; the aligned time grid, as R8.
  - **Output and outcomes:** batch output, as R9; the outcomes and exit codes, as R10.
  - **Fallback (R11):** the source is `essCurrentPowerPlanIdentifier` if set, otherwise `essResultPowerPlanIdentifier`
    (A4's own previous plan).
  - **Code (R12–R13):** code structure and cleanup; dependencies pinned to the A5 versions.
- **R2. ESS and energy model (as A5-IMP R1, R2, R4).**
  - Stored energy `E[k]` in kWh, starting from the total energy `essEffectiveCharge`/1000.
  - Efficiencies as in A5.
  - Window `[ess_min_soc, ess_max_soc]` % of the capacity, widened to E0 if E0 is already outside.
  - `ch`/`dis` exclusive through a `Binary`.
  - Soft end target, default `E[N] = E0`.
- **R3. PCC.**
  - Balance: `imp − exp = load + pv + ch − dis`, pv negative.
  - Limits: `imp ≤ pccImportLimitW/1000 + oImp` and `exp ≤ −pccExportLimitW/1000 + oExp` (soft).
  - No import/export binaries: the objective has no price, so importing and exporting at once never lowers it.
- **R4. Objective (A4's goal, unchanged in meaning, in kWh).** Minimise Σ_k Δt · ( `exp` + `ch·essDegradationWeight`
  + `(oImp + oExp)·pccLimitPenaltyPerKWh` ) + `|E[N] − Eend|·endEnergyPenaltyPerKWh`.
- **R5. Parameters.**
  - **Required:** `apiEndpoint`, `clientId` (or `clientID`), `token`, `consumptionPrognosisIdentifier`,
    `productionPrognosisIdentifier`, `essEffectiveCharge`, `pccImportLimitW`, `pccExportLimitW`, `ess_min_soc`,
    `ess_max_soc`, `essResultPowerPlanIdentifier`, `interval`, `min_period`. Also **either** `ess_max_P_dp_id` or
    `essPowerLimitW`, and **either** `ess_max_E_dp_id` or `essMaxCharge`.
  - **Optional (default):** `essEffectiveEndCharge` [start energy], `essCurrentPowerPlanIdentifier` [result id],
    `essChargeEfficiency`/`essDischargeEfficiency` [0.95], `essDegradationWeight` [1e-6],
    `pccLimitPenaltyPerKWh` [1000], `endEnergyPenaltyPerKWh` [10], `solverTimeLimitS` [300], `maxInputAgeS` [900],
    `lokiUrl`, `writeDebugFile` [on-failure].
  - `npPriceIdentifier` and `essMinSafeLim` are accepted and ignored, with an INFO line.
  - Range checks as in A5-IMP R6.

## 4. Acceptance criteria

- **AC1 (pytest, Python 3.11 + GLPK).**
  - (a) Paging, the time grid, input errors, configuration errors, the `clientID` alias.
  - (b) A model case: PV surplus at midday, load otherwise, room in the battery. The plan stores the surplus instead of
    exporting it (export 0 while the window allows), and E stays in `[Emin, Emax]`.
  - (c) Efficiency is applied.
  - (d) A soft PCC limit is reported.
  - (e) The end option is honoured.
  - (f) Fallback on too little data and on a failed solve; exit 1 with nothing to fall back on; a failed login gives
    exit 1 with no data request.
- **AC2.** `py_compile` passes; no `import *`; no assignment to `np`, `pd` or `value`.
- **AC3 (live, DSxOS-NRG).** With its own credential and output to `ess_p_a4_test`:
  - exit 0;
  - a plan over the common horizon (more than 20 h, interval 60, aligned start);
  - the SoC implied by the plan stays within 15–80 %;
  - the run is shorter than the cron period.
  - No device write.

## 5. Out of scope

Price-based objectives (that is A5); credentials from the site secrets store; the platform's core `A4` task.

## 6. Amendments

- **A4-A1 (2026-11-14, owner decision "penalise losses", A4 only).** Finding from the first live run on NRG
  (13:45Z): with no PV surplus anywhere in the horizon, the plan discharged 118.7 kWh to 15 % SoC and recharged
  131.6 kWh from the grid (about 13 kWh lost) although idling was optimal. Cause: `essDegradationWeight` 1e-6 times
  Δt (1/60 h) is below the solver tolerance, so cycling was free.
  - R4 gains a loss term: Σ_k Δt · `essLossWeight` · ( `ch·(1 − η_ch)` + `dis·(1/η_dis − 1)` ), the conversion
    losses in kWh. New optional parameter `essLossWeight` [2.0], must be ≥ 0. At 1.0, burning surplus by cycling
    would tie with exporting it; 2.0 prefers export wherever the export limit allows, while storing surplus for later
    use (about 0.1 kWh lost per kWh, 0.2 cost) still beats exporting (1.0). Within an export limit, burning is still
    chosen over a limit overrun.
  - The result reports the loss energy (`loss_kwh`) and the run log shows it.
  - AC1 gains (g): with no PV surplus and end target = start, the plan is idle (every step 0, losses 0); and (h): with
    a full battery and η 0.95, surplus that may be exported is exported, not burned (replaces the η = 1.0 workaround
    in the full-battery test).
