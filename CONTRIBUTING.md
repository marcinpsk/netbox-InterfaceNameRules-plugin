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
4. Run tests: `python manage.py test netbox_interface_name_rules`
5. Open a PR to `develop`

### Commits

We use [Conventional Commits](https://www.conventionalcommits.org/):

- `feat:` — new feature (triggers minor version bump)
- `fix:` — bug fix (triggers patch version bump)
- `docs:`, `ci:`, `chore:`, `refactor:`, `test:` — no version bump

## License

By contributing, you agree that your contributions are licensed under Apache-2.0.
