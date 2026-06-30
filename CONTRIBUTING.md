# Contributing

Thanks for your interest in improving the AI Runtime Platform.

## Development setup

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pre-commit install   # optional, runs ruff on commit
```

## Before opening a pull request

Run the full local gate; it must pass:

```sh
make validate   # ruff check + ruff format --check + pytest + helm lint + kustomize render
```

Useful individual targets: `make test`, `make lint`, `make fmt`, `make fmt-check`,
`make helm-lint`, `make render`.

## Conventions

- **Commits**: Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `chore:`, `test:`).
- **Code style**: enforced by `ruff` (lint + format). Run `make fmt` before committing.
- **Comments and identifiers**: English only.
- **Diffs**: keep them minimal and focused; avoid unrelated reformatting.
- **Tests**: add or update tests for behavioural changes; gateway tests live in `app/gateway/tests/`.

## Branches and PRs

Work on a feature branch and open a PR against `main`. CI (`python`, `manifests`,
`docker`) must be green before merge.
