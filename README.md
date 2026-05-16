# liquidation-trading

Initial Django project scaffold for the liquidation trading app.

This project follows the liquidation-based trading concept described in
[`docs/liquidation_framework_concept.md`](docs/liquidation_framework_concept.md).

## Local Services

Start Postgres and Redis:

```bash
docker compose up -d
```

Install Python dependencies:

```bash
poetry install
```

Run Django checks:

```bash
poetry run python manage.py check
```

Install pre-commit hooks:

```bash
poetry run pre-commit install
```

Run pre-commit manually:

```bash
poetry run pre-commit run --all-files
```

Pre-commit runs basic file checks, Ruff lint/import fixes, and Black formatting.

or just run:

```bash
pre-commit
```

## Create a superuser

```bash
poetry run python manage.py createsuperuser
```

## Logging

Fetch, backfill, refresh, and OI-aggregation controllers emit progress to
stdout via Python's `logging` module. The threshold is `INFO` by default
— enough to watch a year-long backfill stream day-by-day — and is
controlled by `DJANGO_LOG_LEVEL` (e.g. `DJANGO_LOG_LEVEL=DEBUG` adds
plumbing-level archive URLs; `WARNING` silences the per-step lines).

The Binance metrics archive (OI history) is fetched in parallel; tune the
worker count with `BINANCE_ARCHIVE_CONCURRENCY` (default 8, bounded to
[1, 32]) — Binance publishes no monthly metrics ZIPs, so a 1-year OI
backfill is 365 daily HTTP requests and connection concurrency is the
only lever for wall-clock.

## Chart APIs

The `chart` app exposes a small set of read-only JSON endpoints feeding
the candlestick page. Routes are mounted at the project root under
namespace `chart` (`core/urls.py`):

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Candlestick chart page. |
| `/api/candles/<symbol>/<interval>/` | GET | DB candles, auto-fetching when the table is empty. |
| `/api/refresh/<symbol>/<interval>/` | POST | Run the full 15m refresh bundle (candles 5m/15m/4h/1d, OI 5m + derived 1h, funding) and return the 15m series. |
| `/api/oi/<symbol>/<period>/` | GET | Open Interest series for the indicator sub-pane. |
| `/api/funding/<symbol>/` | GET | Funding settlements for the indicator sub-pane. |
| `/api/cvd/<symbol>/<interval>/` | GET | Windowed CVD anchors for the indicator sub-pane. |
| `/api/clusters/<symbol>/` | GET | §5 liquidation cluster map (zones + projections + heatmap). Accepts `?lookback_days=N` (default 7, clamped to [1, 7] — the doc-recommended range per §5.2 / §12.3). |

The cluster endpoint is computed on-demand from existing 1h OI, 1h
candles, and funding rates already in the DB — no upstream fetches —
mirroring how CVD is served. See
[`docs/liquidation_framework_concept.md`](docs/liquidation_framework_concept.md)
§5 for the underlying methodology.

## Agent Guidance

Agent-specific project guidance lives in [`AGENTS.md`](AGENTS.md). Keep this README
updated whenever setup steps, commands, services, structure, or developer workflow
change.
