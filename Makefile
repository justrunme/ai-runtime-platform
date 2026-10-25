PYTHON ?= python3

.DEFAULT_GOAL := help

.PHONY: help test lint fmt fmt-check helm-lint render validate

help:
	@printf '%s\n' 'Targets: test lint fmt fmt-check helm-lint render validate fuzz-test fuzz-intensive'

test:
	$(PYTHON) -m pytest

fuzz-test:
	$(PYTHON) -m pytest app/gateway/tests/fuzz/

fuzz-intensive:
	$(PYTHON) -m pytest app/gateway/tests/fuzz/ --hypothesis-seed=random -v

lint:
	$(PYTHON) -m ruff check app experiments

fmt:
	$(PYTHON) -m ruff format app experiments

fmt-check:
	$(PYTHON) -m ruff format --check app experiments

helm-lint:
	helm lint charts/vllm-runtime

render:
	kubectl kustomize deploy/base >/dev/null

validate: lint fmt-check test helm-lint render
