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

A local run prints coverage but does not fail on it. The 97% gate runs in CI on the combined
coverage data of the NetBox v4.5.3 leg and the netbox-branching leg.

### Commits

We use [Conventional Commits](https://www.conventionalcommits.org/):

- `feat:` — new feature (triggers minor version bump)
- `fix:`, `perf:`, `refactor:` — bug fix, performance or refactoring change (triggers patch version bump)
- `docs:`, `ci:`, `chore:`, `test:`, `build:`, `style:`, `revert:` — no version bump

The repository merges every PR with a merge commit; squash and rebase merges are off. The release tool
ignores merge commits and writes one changelog line for each commit on the branch. Write each commit
subject as the changelog line you want users to read.

## Releases

A push to `main` runs python-semantic-release. It reads the Conventional Commit subjects since the last
tag, selects the version bump, and writes the release section of `CHANGELOG.md`.

- Do not edit `CHANGELOG.md` by hand, and do not add an `## Unreleased` section. The release tool
  inserts each new version above the older ones, so a hand-written section stays behind and keeps
  describing changes that already shipped. A documentation test refuses any section that is not a
  release section.
- The release PR from `develop` to `main` also uses a merge commit. A squash gives the release tool one
  commit, and the release then depends on the squash message. `parse_squash_commits = true` in
  `pyproject.toml` makes the tool parse each Conventional Commit it finds in that message. The v1.5.4
  squash message held only the PR title, so v1.5.4 got one `fix` line and a patch bump for changes
  that included features.

## License

By contributing, you agree that your contributions are licensed under Apache-2.0.
