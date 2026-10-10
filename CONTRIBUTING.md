# Contributing

## Development setup

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pre-commit install   # optional; runs ruff on commit
```

## Branch naming

- `feat/<short-name>`
- `fix/<short-name>`
- `chore/<short-name>`
- `docs/<short-name>`
- `ci/<short-name>`

## Commits

Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `chore:`, `test:`, `ci:`).

## Tests

```sh
make validate   # ruff check + format --check + pytest + helm lint + kustomize render
```

Useful targets: `make test`, `make lint`, `make fmt`, `make helm-lint`, `make render`.

Gateway tests live in `app/gateway/tests/`.

## Pull requests

Work on a feature branch; open a PR against `main`. CI (`python`, `manifests`, `docker`) must be green. Prefer squash merge.

Checklist:

- [ ] `make validate` passes
- [ ] Branch uses `feat/` / `fix/` / `chore/` / `docs/` / `ci/`
- [ ] Conventional commits
- [ ] Behavioural changes include tests
- [ ] English-only comments and identifiers
