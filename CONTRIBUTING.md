# Contributing

## Branch targets

- **Code changes** (features, fixes, refactoring) → PR to `develop`
- **Contrib rules** (new/updated YAML in `contrib/`) → PR to `develop`

`main` is release-only — PRs to `main` come from `develop` only.

## Adding interface name rules

1. Fork the repo and create a branch
2. Add or edit the vendor YAML file in `contrib/`
3. Follow the existing format (see `contrib/README.md`)
4. Run `yamllint` on your file
5. Open a PR to `develop`

## Code contributions

### Setup

```bash
git clone <your-fork>
cd netbox-InterfaceNameRules-plugin
pip install uv
uv sync
pre-commit install
```

### Workflow

1. Create a branch from `develop`
2. Make your changes
3. Run `pre-commit run --all-files`
4. Run tests with pytest (see [Running tests](#running-tests))
5. Open a PR to `develop`

### Running tests

The suite runs under pytest only. `manage.py test` does not load the `conftest.py` fixtures, so
some tests fail under it and some guards are off.

Inside the devcontainer, `netbox-test` runs the full suite. Give it a pytest node ID to run one test:

```bash
netbox-test
netbox-test netbox_interface_name_rules/tests/test_views.py::TestClassName::test_method_name
```

Outside the devcontainer, run pytest from the plugin root. Set `TEST_DB_NAME` to a name that starts
with `test_`, and set `TEST_REDIS_HOST` to the Redis server that the tests can use. If NetBox is not at
`/opt/netbox/netbox`, add `-o pythonpath=/path/to/netbox/netbox`.

```bash
TEST_DB_NAME=test_netbox_interface_name_rules TEST_REDIS_HOST=localhost pytest netbox_interface_name_rules
```

### Commits

We use [Conventional Commits](https://www.conventionalcommits.org/):

- `feat:` — new feature (triggers minor version bump)
- `fix:` — bug fix (triggers patch version bump)
- `docs:`, `ci:`, `chore:`, `refactor:`, `test:` — no version bump

## License

By contributing, you agree that your contributions are licensed under Apache-2.0.
