# dsxos-app-A4 - Self-consumption maximisation

A4 plans the power of an energy storage system (ESS) so that as little PV energy as possible is exported: surplus PV
is stored and used later. The plan is written as a prognosis of a datapoint (W, positive = charging). A4 runs as a
DSxOS python app on its `cron`.

Requirements: [docs/requirements-a4-nrg-integration.md](docs/requirements-a4-nrg-integration.md) (A4-IMP; it reuses
the decisions of dsxos-app-A5 A5-IMP and A5-AUTH).

## Model

Per time step (`interval`):
- **ESS:** charge in [0, Pch], discharge in [0, Pdis] (separate limits, both positive), never both. Stored energy `E[k+1] = E[k] + (ch·η_ch − dis/η_dis)·Δt`,
  within [`ess_min_soc`, `ess_max_soc`] % of the capacity (widened to the start value if it is already outside).
- **PCC:** net = import − export = load + pv + ESS, with pv negative for generation. The net power is kept in a
  signed window `[pccExportLimitW, pccImportLimitW]` by soft limits (penalty, WARNING): `net ≤ U + oU`,
  `net ≥ L − oL`. `L ≤ 0` is an export limit; `L > 0` is a minimum import.
- **End:** the end energy should equal the start, or the value of `essEffectiveEndCharge` if set (soft).
- **Minimised (kWh):** export + `essLossWeight` · conversion losses (`ch·(1−η_ch) + dis·(1/η_dis−1)`) +
  `essDegradationWeight` · charged energy + penalties. The loss term keeps the battery idle when nothing can be
  gained and prefers exporting to burning surplus where export is allowed (amendment A4-A1). A small
  `essRampWeight` · Σ |ESS power change between steps| breaks ties between equal-cost timings, so the plan is flat
  instead of flickering on/off (A4-AF).

The time grid starts at the run time rounded down to `interval`. With too little forecast, or no usable solver
result, A4 re-posts the remaining part of its previous plan (fallback). Exit 0 = plan or fallback posted; 1 = error.

## Configuration (`params`)

See [example_config.yaml](example_config.yaml).

| Param | Unit | Default | Meaning |
|---|---|---|---|
| `apiEndpoint`, `clientId` (legacy `clientID`), `token` | | required | Data server API and credential (client ID + secret) |
| `consumptionPrognosisIdentifier` / `productionPrognosisIdentifier` | W | required | Load / PV forecast (PV negative) |
| `ess_max_P_charge_dp_id`, `ess_max_P_discharge_dp_id` | W | required | Charge / discharge power limit, positive magnitudes: datapoint identifier or number (may be the same datapoint) |
| `ess_max_E_dp_id` | Wh | required | ESS capacity: datapoint identifier or number |
| `ess_min_soc`, `ess_max_soc` | % | required | SoC window: datapoint identifier or number; `0 ≤ min < max ≤ 100` |
| `pccImportLimitW` | W | required | Upper limit of the net PCC power (import positive): datapoint identifier or number |
| `pccExportLimitW` | W | required | Lower limit of the net PCC power, signed (negative = export allowed, 0 = no export, positive = minimum import): datapoint identifier or number; must be ≤ `pccImportLimitW` |
| `essEffectiveCharge` | Wh | required | Datapoint: total stored energy now (at most `maxInputAgeS` old) |
| `essEffectiveEndCharge` | Wh | start energy | Optional datapoint: end target |
| `essResultPowerPlanIdentifier` | W | required | Datapoint that receives the plan |
| `essCurrentPowerPlanIdentifier` | W | result datapoint | Datapoint whose latest plan is the fallback |
| `interval`, `min_period` | s | required | Time step; minimum forecast horizon |
| `essChargeEfficiency`, `essDischargeEfficiency` | | 0.95 | Efficiencies |
| `essLossWeight` | | 2.0 | Weight per kWh of conversion losses |
| `essDegradationWeight` | | 1e-6 | Weight per kWh charged |
| `essRampWeight` | | 0.001 | Weight per kW of ESS power change between consecutive steps (not scaled by `interval`); 0 = off |
| `pccLimitPenaltyPerKWh`, `endEnergyPenaltyPerKWh` | | 1000, 10 | Penalties per kWh |
| `solverTimeLimitS`, `maxInputAgeS` | s | 300, 900 | GLPK limit; max age of the stored-energy reading |
| `lokiUrl`, `writeDebugFile` | | Loki on localhost, `on-failure` | Logging; debug file `on-failure`/`always`/`never` |

**Operational parameters** (limits, SoC window, power, capacity; the seven rows above from
`ess_max_P_charge_dp_id` to `pccExportLimitW`): a string is a datapoint identifier whose latest value
(`lastReadingValue`, no staleness check) is read at every run; a number is a constant. A missing datapoint or one
without a value is an input error (exit 1, nothing posted). Values are validated after resolving (powers and capacity
> 0, `0 ≤ ess_min_soc < ess_max_soc ≤ 100`, `pccExportLimitW ≤ pccImportLimitW`): a violation is a configuration error
for a number and an input error for a datapoint. The resolved values and their sources (`dp <id>` or `const`) are logged
once per run. There are no defaults for these.

**Removed parameters:** the unused and the superseded single-limit/fixed-capacity params of earlier versions (listed in
`REMOVED` in `main.py`, see OPD R5) are a configuration error naming the replacement if present in the YAML; a stale
config does not run with silently different limits.

## Tests

`pip install -r requirements-dev.txt && pytest` (the model tests need `glpsol`, apt package `glpk-utils`, see
`packages.txt`).
