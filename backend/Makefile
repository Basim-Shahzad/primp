.PHONY: install lint typecheck test cov run worker beat migrate openapi bench check
SETTINGS ?= config.settings.dev

install:
	uv sync --all-extras

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run lint-imports

typecheck:
	uv run mypy

test:
	uv run pytest -q

cov:
	uv run pytest -q --cov --cov-report=term-missing --cov-fail-under=90

run:
	DJANGO_SETTINGS_MODULE=$(SETTINGS) uv run python manage.py runserver 0.0.0.0:8000

worker:
	DJANGO_SETTINGS_MODULE=$(SETTINGS) uv run celery -A config worker -l info -Q critical,default,bulk

beat:
	DJANGO_SETTINGS_MODULE=$(SETTINGS) uv run celery -A config beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler

migrate:
	DJANGO_SETTINGS_MODULE=$(SETTINGS) uv run python manage.py migrate

openapi:
	DJANGO_SETTINGS_MODULE=config.settings.test uv run python manage.py export_openapi > openapi.json

bench:
	uv run pytest -q -m slow -s

check: lint typecheck test
