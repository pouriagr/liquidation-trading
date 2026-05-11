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

## Agent Guidance

Agent-specific project guidance lives in [`AGENTS.md`](AGENTS.md). Keep this README
updated whenever setup steps, commands, services, structure, or developer workflow
change.
