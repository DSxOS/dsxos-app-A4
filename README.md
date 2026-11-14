# dsxos-app-A4 - Self-consumption maximisation

A4 plans the power of an energy storage system (ESS) so that as little PV energy as possible is exported: surplus PV
is stored and used later. The plan is written as a prognosis of a datapoint (W, positive = charging). A4 runs as a
DSxOS python app on its `cron`.

Requirements: [docs/requirements-a4-nrg-integration.md](docs/requirements-a4-nrg-integration.md) (A4-IMP; it reuses
the decisions of dsxos-app-A5 A5-IMP and A5-AUTH).

## Model

Per time step (`interval`):
- **ESS:** charge/discharge in [0, Pmax], never both. Stored energy `E[k+1] = E[k] + (ch·η_ch − dis/η_dis)·Δt`,
  within [`ess_min_soc`, `ess_max_soc`] % of the capacity (widened to the start value if it is already outside).
- **PCC:** import − export = load + pv + ESS, with pv negative for generation. Import/export limits are soft
  (penalty, WARNING).
- **End:** the end energy should equal the start, or the value of `essEffectiveEndCharge` if set (soft).
- **Minimised (kWh):** export + `essLossWeight` · conversion losses (`ch·(1−η_ch) + dis·(1/η_dis−1)`) +
  `essDegradationWeight` · charged energy + penalties. The loss term keeps the battery idle when nothing can be
  gained and prefers exporting to burning surplus where export is allowed (amendment A4-A1).

The time grid starts at the run time rounded down to `interval`. With too little forecast, or no usable solver
result, A4 re-posts the remaining part of its previous plan (fallback). Exit 0 = plan or fallback posted; 1 = error.

## Configuration (`params`)

See [example_config.yaml](example_config.yaml).

| Param | Unit | Default | Meaning |
|---|---|---|---|
| `apiEndpoint`, `clientId` (legacy `clientID`), `token` | | required | Data server API and credential (client ID + secret) |
| `consumptionPrognosisIdentifier` / `productionPrognosisIdentifier` | W | required | Load / PV forecast (PV negative) |
| `ess_max_P_dp_id` or `essPowerLimitW` | W | one required | ESS power limit: datapoint, or a fixed number |
| `ess_max_E_dp_id` or `essMaxCharge` | Wh | one required | ESS capacity: datapoint, or a fixed number |
| `essEffectiveCharge` | Wh | required | Datapoint: total stored energy now (at most `maxInputAgeS` old) |
| `essEffectiveEndCharge` | Wh | start energy | Optional datapoint: end target |
| `ess_min_soc`, `ess_max_soc` | % | required | SoC window |
| `pccImportLimitW`, `pccExportLimitW` | W | required | PCC limits; export as a number ≤ 0 (0 = no export) |
| `essResultPowerPlanIdentifier` | W | required | Datapoint that receives the plan |
| `essCurrentPowerPlanIdentifier` | W | result datapoint | Datapoint whose latest plan is the fallback |
| `interval`, `min_period` | s | required | Time step; minimum forecast horizon |
| `essChargeEfficiency`, `essDischargeEfficiency` | | 0.95 | Efficiencies |
| `essLossWeight` | | 2.0 | Weight per kWh of conversion losses |
| `essDegradationWeight` | | 1e-6 | Weight per kWh charged |
| `pccLimitPenaltyPerKWh`, `endEnergyPenaltyPerKWh` | | 1000, 10 | Penalties per kWh |
| `solverTimeLimitS`, `maxInputAgeS` | s | 300, 900 | GLPK limit; max age of the stored-energy reading |
| `lokiUrl`, `writeDebugFile` | | Loki on localhost, `on-failure` | Logging; debug file `on-failure`/`always`/`never` |
| `npPriceIdentifier`, `essMinSafeLim` | | | Accepted, ignored |

## Tests

`pip install -r requirements-dev.txt && pytest` (the model tests need `glpsol`, apt package `glpk-utils`, see
`packages.txt`).
