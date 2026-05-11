# AGENTS.md

Guidance for AI agents working in this repository.

## Project Context

- This is a Django project for liquidation-based trading research and tooling.
- The Django project/config package is `core`.
- Domain functionality lives in three Django apps: `data` (ingestion + storage), `feature` (derived signals computed from `data`'s tables), and `chart` (read-only UI/API on top of `data` and `feature`).
- The project follows the trading concept described in `docs/liquidation_framework_concept.md`.
- Keep implementation decisions aligned with that concept document unless the user explicitly asks to change direction.

## Project Structure

Top-level layout:

```
liquidation-trading/
├── core/                       # Django project package (settings, URLs, ASGI/WSGI)
├── data/                       # App: market-data ingestion and persistence
├── feature/                    # App: derived signals computed from `data` tables
├── chart/                      # App: HTML page + JSON APIs for charts
├── docs/                       # Concept and design docs
│   └── liquidation_framework_concept.md
├── manage.py
├── docker-compose.yml          # Postgres + Redis for local dev
├── pyproject.toml              # Poetry config (packages: core, data)
├── poetry.lock
├── .pre-commit-config.yaml     # Ruff + Black + basic hooks
├── .env / .env.example
├── AGENTS.md                   # This file
└── README.md
```

### `core/` — Django project package

- `settings.py` — env-driven settings; Postgres via `psycopg`, Redis cache via `django.core.cache.backends.redis`. `INSTALLED_APPS` includes `data` and `chart`.
- `urls.py` — mounts `admin/` and includes `chart.urls` at the root under namespace `chart`.
- `asgi.py`, `wsgi.py` — standard Django entry points.

### `data/` — ingestion + storage app

Models, controllers, and management commands are split into packages rather than single-file modules.

```
data/
├── apps.py
├── admin.py
├── models/                     # One file per model; package re-exports keep `from data.models import X` stable
│   ├── __init__.py             # Re-exports: Candle, FundingRate, OpenInterest, Symbol, Interval
│   ├── candle.py
│   ├── funding_rate.py
│   ├── open_interest.py
│   └── choices.py              # Symbol / Interval enums shared across models
├── controllers/                # External-API / archive controllers (Binance, etc.)
│   ├── __init__.py             # Builds module-level singletons (binance_*_controller)
│   ├── binance_candles.py
│   ├── binance_funding_rate.py
│   ├── binance_open_interest.py
│   ├── binance_klines_archive.py
│   └── binance_metrics_archive.py
├── management/
│   └── commands/               # `python manage.py <name>` entry points
│       ├── fetch_binance_candles.py
│       ├── fetch_binance_open_interest.py
│       ├── fetch_binance_funding_rate.py
│       ├── backfill_binance_candles.py
│       └── backfill_binance_open_interest.py
└── migrations/
```

Conventions for `data/`:

- Add a new model in its own file under `data/models/`, then re-export it from `data/models/__init__.py`. Put shared enums/choices in `data/models/choices.py`.
- Add a new external-data integration as a controller class in `data/controllers/<source>_<thing>.py` and expose a singleton from `data/controllers/__init__.py` (`<source>_<thing>_controller`). Callers should use the singleton, not instantiate controllers ad-hoc.
- Add Django management commands under `data/management/commands/`. Use `fetch_*` for incremental/live pulls and `backfill_*` for historical/archive ingestion.

### `feature/` — derived-signal app

Derived signals (computed from rows already in `data/`'s tables, not from any
external source) live here. Same package-style layout as `data/`, plus a
`services/` package for tiny pure-Python helpers (Django-free formulas) and a
`signals.py` for wiring those helpers onto `data` model writes.

```
feature/
├── apps.py                     # FeatureConfig.ready() imports signals.py
├── admin.py
├── signals.py                  # pre_save handlers that populate `data` columns
├── models/                     # Empty today; reserved for any persisted features
├── services/                   # Pure-Python helpers (no Django imports)
│   ├── delta.py                # canonical compute_delta(...)
│   └── oi.py                   # aggregate_5m_to_1h(...) for derived 1h OI
├── controllers/                # Readers, derivers, and cross-source orchestrators
│   ├── __init__.py             # Builds module-level singletons
│   ├── cvd.py                  # CVDController.latest() / .series()
│   ├── oi_aggregator.py        # OIAggregatorController.aggregate(...)
│   └── refresh.py              # RefreshController.refresh(...)  — full 15m bundle
└── migrations/
```

Conventions for `feature/`:

- A `feature` controller takes data exclusively from `data/`'s models or by
  *delegating* to `data.controllers` singletons; it must not make direct
  external HTTP calls or open archive files of its own. (Raw HTTP is the
  data controllers' job — see `RefreshController` for the canonical pattern
  of orchestrating across data controllers.)
- Reuse `Symbol` and `Interval` from `data.models.choices`. Do not duplicate
  the symbol/interval allowlists inside `feature/`.
- **Dependency direction is `feature → data` only.** If a derived value needs
  to live on a `data` model (e.g. `Candle.delta`, or `OpenInterest(period='1h')`),
  put the *formula* in `feature/services/`, the *wiring* in either
  `feature/signals.py` or a `feature/controllers/<x>.py` (whichever is
  natural), and the *storage* on the `data` model — `data` must never import
  from `feature`. This keeps `data` independently usable even if `feature`
  is removed or temporarily disabled.
- Naming: one controller per file under `feature/controllers/`, exposed as a
  singleton from `feature/controllers/__init__.py`. On-demand readers (which
  compute, not persist) don't need a management command — just call the
  controller. Reserve the `backfill_*` command prefix for the case where
  `feature` does persist a precomputed table.
- **The 15m bundle is the unit of "refresh".** Per the framework's decision
  rhythm (`docs/liquidation_framework_concept.md` §12.3), trading at 15m
  requires a fixed bundle of inputs at their own resolutions: candles
  (5m, 15m, 4h, 1d), OI (5m raw + 1h derived), funding (full history).
  `feature.controllers.refresh_controller.refresh(symbol, "15m")` is the
  single entry point for refreshing all of them — never wire chart/UI code
  to the per-source controllers directly for a refresh action.

### `chart/` — read-only UI + JSON APIs

```
chart/
├── apps.py
├── urls.py                     # namespace `chart`: home + 2 JSON APIs
├── views.py
├── serializers.py
├── templates/chart/
│   └── home.html
└── static/chart/
    ├── css/
    └── js/
```

Conventions for `chart/`:

- `chart` is a presentation app — it reads from `data`'s models/controllers and must not own persistence.
- Routes live under namespace `chart` (`{% url 'chart:home' %}`, `chart:candles`, `chart:refresh`).
- App-scoped templates and static files use the `chart/<file>` subfolder pattern so Django's loaders namespace them correctly.

### `docs/`

- `liquidation_framework_concept.md` is the source of truth for trading concepts. Link to it from code/docs when behavior derives from it.

## Dependency Management

- Use Poetry for Python dependency management.
- Add runtime dependencies with `poetry add <package>`.
- Add development dependencies with `poetry add --group dev <package>`.
- Do not create or use `requirements.txt` unless the user explicitly requests it.

## Local Services

- Docker Compose provides Postgres and Redis under the project name `liquidation-trading`.
- Postgres container: `liquidation-trading-postgres`.
- Redis container: `liquidation-trading-redis`.

## Important Commands

```bash
poetry install
docker compose up -d
poetry run python manage.py check
poetry run pre-commit install
poetry run pre-commit run --all-files
```

## Documentation Rule

- Always update `README.md` when adding, changing, or removing setup steps, commands, services, project structure, or developer workflow.
- If behavior is based on the trading framework, link back to `docs/liquidation_framework_concept.md` where helpful.

## Django Conventions

- Keep project-level settings, URLs, ASGI, and WSGI in `core`.
- Keep domain-specific functionality in Django apps: ingestion/persistence in `data`, derived-signal computation in `feature`, presentation in `chart`.
- Follow the package-style layout for `models/`, `controllers/`, and `management/commands/` described in *Project Structure* — one file per model/controller/command, re-exported from the package `__init__.py` where applicable. The same layout applies to `data/` and `feature/`.
- Avoid adding models, views, URLs, or business logic unless the user asks for them.

## Quality Checks

- After code or config changes, run the most relevant checks:

```bash
poetry run python manage.py check
poetry run pre-commit run --all-files
```
