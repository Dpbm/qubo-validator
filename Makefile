.PHONY: add
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
	uv run python -m pytest ./tests

lint:
	@echo "Linting..."
	uv run ruff check --fix

%:
	@:
