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

## Agent Guidance

Agent-specific project guidance lives in [`AGENTS.md`](AGENTS.md). Keep this README
updated whenever setup steps, commands, services, structure, or developer workflow
change.
