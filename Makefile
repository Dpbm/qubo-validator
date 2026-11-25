.PHONY: add add-dev test lint run
.SILENT:
.ONESHELL:
SHELL = /usr/bin/bash

add:
	DEPENDENCIES="$(filter-out $@, $(MAKECMDGOALS))"
	@echo "ADDING dependendencies: $$DEPENDENCIES"
	echo $$DEPENDENCIES | tr " " "\n" >> requirements.txt
	uv add $$DEPENDENCIES

add-dev:
	DEPENDENCIES="$(filter-out $@, $(MAKECMDGOALS))"
	@echo "ADDING -DEV- dependendencies: $$DEPENDENCIES"
	echo $$DEPENDENCIES | tr " " "\n" >> dev-requirements.txt
	uv add --dev $$DEPENDENCIES

test:
	@echo "Running tests..."
	QT_QPA_PLATFORM=offscreen uv run python -m pytest -s -o faulthandler_timeout=5 -vvv ./tests

lint:
	@echo "Linting..."
	uv run ruff check --fix

run:
	uv run qubo-validator

%:
	@:
