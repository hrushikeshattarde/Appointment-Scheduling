.PHONY: install lint typecheck test check run-help

install:
	uv sync --all-extras

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

format:
	uv run ruff check --fix src tests
	uv run ruff format src tests

typecheck:
	uv run mypy

test:
	uv run pytest

check: lint typecheck test

run-help:
	uv run facility-profiles --help
