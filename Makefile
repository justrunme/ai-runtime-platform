.DEFAULT_GOAL := help

.PHONY: help test lint fmt fmt-check helm-lint render validate

help:
	@printf '%s\n' 'Targets: test lint fmt fmt-check helm-lint render validate'

test:
	python -m pytest

lint:
	python -m ruff check app

fmt:
	python -m ruff format app

fmt-check:
	python -m ruff format --check app

helm-lint:
	helm lint charts/vllm-runtime

render:
	kubectl kustomize deploy/base >/dev/null

validate: lint fmt-check test helm-lint render
