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

## Property-Based Fuzz Testing

The repository includes property-based fuzz tests using Hypothesis for security-critical input parsers. These tests are located in `app/gateway/tests/fuzz/` and run automatically with the standard test suite.

To run fuzz tests with a larger example budget for deeper exploration:

```sh
pytest app/gateway/tests/fuzz/ --hypothesis-seed=random -v
```

Or use the Makefile target:

```sh
make fuzz-intensive
```

The default test suite runs each fuzz test with 100-300 examples, completing in under 20 seconds. For more thorough testing, you can temporarily increase `max_examples` in individual test files.

## Pull requests

Work on a feature branch; open a PR against `main`. CI (`python`, `manifests`, `docker`) must be green. Prefer squash merge.

Checklist:

- [ ] `make validate` passes
- [ ] Branch uses `feat/` / `fix/` / `chore/` / `docs/` / `ci/`
- [ ] Conventional commits
- [ ] Behavioural changes include tests
- [ ] English-only comments and identifiers
