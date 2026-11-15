# BNPL Daily Order Forecasting Engine

Daily forecasting engine for **successful & finalized BNPL orders** (ClickHouse
DWH → SQL extraction → Python forecasting), per
`uploads/BNPL-Daily-Order-Forecasting-Engine.md`.

This repository currently implements **Phase 0–3 foundations** plus the serving
contract skeleton. It does **not** yet contain the LightGBM/MLForecast models,
conformal intervals, SHAP, lifecycle detection or the scenario engine.

## Architecture boundaries

```
ClickHouse  --SQL-->  extraction  --Python-->  data mart  -->  features
                                                          -->  backtest
                                                          -->  forecasts
```

* SQL lives only in `sql/` (versioned, parameterized templates).
* Python never holds large inline SQL; it renders templates with validated values.
* `TOTAL` is **always** derived from leaf entities — never queried independently.
* Ventures and Verticals are siblings; an order belongs to exactly one leaf.

## Layout

| Path | Purpose |
| --- | --- |
| `configs/` | base, database, target, hierarchy, features, entity master |
| `sql/discovery/` | Phase 0 schema/date/status/mapping validation queries |
| `sql/extraction/v1/` | daily leaf orders, daily leaf users, calendar |
| `src/ingestion/` | ClickHouse client, safe SQL rendering, extraction |
| `src/validation/` | Target Definition + production guards |
| `src/data_mart/` | Entity Master, daily mart with quality flags |
| `src/features/` | calendar, lag, rolling features (leakage-safe) |
| `src/models/` | baseline forecasters |
| `src/evaluation/` | metrics, rolling-origin backtest |
| `src/reconciliation/` | summing matrix, bottom-up / MinTrace / top-down, rounding |
| `src/serving/` | forecast run orchestration + output contract |
| `scripts/` | discover, extract, build mart, backtest, daily forecast, smoke run |
| `tests/` | 81 tests covering target, mart, features, leakage, hierarchy, backtest, serving |

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # fill ClickHouse credentials (read-only user)
export $(grep -v '^#' .env | xargs)
```

Credentials are read only from environment variables — never from YAML or Git.

## Usage

```bash
# Phase 0 — source discovery reports
python scripts/discover_sources.py --start 2024-01-01 --end 2024-07-01

# Phase 1 — historical extraction (monthly partitions, snapshot metadata)
python scripts/extract_history.py --start 2023-01-01 --end 2026-11-01 --monthly

# Phase 2 — daily data mart
python scripts/build_data_mart.py --snapshot artifacts/extracts/<snapshot_id>

# Phase 3 — rolling-origin baseline backtest
python scripts/backtest.py --mart artifacts/marts/<id>/fact_daily_entity_metrics.parquet \
    --horizon 30 --step 7

# Daily forecast (D+1..D+30, reconciled, with intervals)
python scripts/daily_forecast.py --mart <mart.parquet> --origin 2026-11-14 \
    --model seasonal_naive

# End-to-end proof on synthetic data (no ClickHouse needed)
python scripts/smoke_run.py

# Tests
python -m pytest
```

## Target Definition is a draft until business approves

`configs/target.yaml` intentionally holds `null` for three decisions:

| Field | Meaning |
| --- | --- |
| `exclude_deleted` | are soft-deleted orders excluded from the target? |
| `date_field` | `registered_date` or `created_date`? |
| `reversal_policy` | does a later reversed/refunded order stay in history? |

`scripts/extract_history.py` **refuses to run** unless these are resolved and
`definition_status: APPROVED` (`--allow-draft` exists for development only).
The Commercial-only exclusions (`Snapp-Venture`, `in_store`, `social`,
`ventures`) are deliberately **not** applied to the engine target; they belong
to a separate reporting scope.

## Data quality semantics

`src/data_mart/orders.py` distinguishes, per entity-day:

* `OBSERVED` — genuine value, including a true zero;
* `MISSING_SOURCE` — no source data for that date → value stays unknown (never 0);
* `ENTITY_NOT_ACTIVE` / `ENTITY_DEACTIVATED` — outside the entity's validity window.

Unmapped orders are returned by SQL as `entity_type = 'UNMAPPED'` and written to
`unmapped_orders.csv`; they are never silently dropped.

## Known limitations / open questions

1. **ClickHouse was not reachable in this environment.** All SQL is written from
   the architecture document; it must be validated by `scripts/discover_sources.py`
   before any production extraction.
2. **`commercial_data.venture_id` uniqueness is unproven.** `daily_leaf_orders.sql`
   groups by `venture_id` with `HAVING count() = 1`, so duplicate mappings are
   excluded from classification (they surface as `UNMAPPED`) until a real
   effective-dated/version column is confirmed. No timestamp column was invented.
3. **The entity master contains only the seven verticals.** Real venture IDs must
   be added from `fms_credit.tbl_venture` + `commercial_data` (Venture IDs are
   stable, not names).
4. **The venture classification rule (`vertical = 'Snapp-Venture' OR
   category = 'ventures'`) is unverified** and may overlap; it is applied once
   via `multiIf`.
5. **User-level features are per-leaf only.** Total-level user metrics must be
   recomputed over the full population (one wallet can transact in several
   entities); the current serving layer only forecasts orders.
6. **Baselines use a fixed-smoothing Holt stand-in for ETS.** Production
   ETS/AutoARIMA come from `statsforecast` in Phase 3.
7. **Intervals are horizon-scaled residual approximations**, not yet conformal
   prediction; coverage has not been measured.
8. **No scheduler, Docker image or FastAPI app yet** (Phase 9).

## Next step

Run `discover_sources.py` against a read-only ClickHouse account, resolve the
three Target Definition decisions, then populate the Entity Master with real
venture IDs before the first historical extraction.
