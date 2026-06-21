.DEFAULT_GOAL := help

.PHONY: help test lint helm-lint render validate

help:
	@printf '%s\n' 'Targets: test lint helm-lint render validate'

test:
	python -m pytest

lint:
	python -m ruff check app

helm-lint:
	helm lint charts/vllm-runtime

render:
	kubectl kustomize deploy/base >/dev/null

validate: lint test helm-lint render
