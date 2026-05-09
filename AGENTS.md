# AGENTS.md

Guidance for AI agents working in this repository.

## Project Context

- This is a Django project for liquidation-based trading research and tooling.
- The Django project/config package is `core`.
- The initial Django app is `data`.
- The project follows the trading concept described in `docs/liquidation_framework_concept.md`.
- Keep implementation decisions aligned with that concept document unless the user explicitly asks to change direction.

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
- Keep domain-specific functionality in Django apps such as `data`.
- Avoid adding models, views, URLs, or business logic unless the user asks for them.

## Quality Checks

- After code or config changes, run the most relevant checks:

```bash
poetry run python manage.py check
poetry run pre-commit run --all-files
```
