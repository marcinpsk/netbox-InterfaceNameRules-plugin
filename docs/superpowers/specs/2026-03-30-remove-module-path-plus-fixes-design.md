# Design: Remove module_path, fix CSV import, add YAML export

**Date:** 2026-03-30
**Branch:** `feat/remove-module-path-plus-fixes`

---

## Problem statement

Three independent improvements bundled into one branch:

1. **Remove `{module_path}` support** — NetBox decided not to implement the `MODULE_PATH_TOKEN` constant, so the feature-detection helper and all related code is dead weight.
2. **Fix KeyError `'Ch'` on CSV import** — Exporting from the rule list and immediately reimporting fails with `KeyError: 'Ch'`. Root cause: the table uses `verbose_name="Ch. Start"` for `channel_start`; django-tables2 uses verbose_names as CSV headers; NetBox's import form normalises headers by splitting on `.`, producing `"Ch"`, which is not a known form field.
3. **Add YAML export** — YAML import already works (via `BulkImportView`). The missing piece is export: a "Download all as YAML" link and a "Export selected as YAML" bulk action.

---

## 1. Remove `{module_path}` support

### Files to change

| File | Change |
|------|--------|
| `netbox_interface_name_rules/utils.py` | Delete the entire file (only contains `supports_module_path()`) |
| `netbox_interface_name_rules/views.py` | Remove `from .utils import supports_module_path` and the `get_extra_context` method on `InterfaceNameRuleListView` |
| `netbox_interface_name_rules/templates/…/interfacenamerule_list.html` | Remove the `{% if supports_module_path %}…{% endif %}` info banner block |
| `netbox_interface_name_rules/engine.py` | Remove the inline comment mentioning `{module_path}` in `has_applicable_interfaces` docstring |
| `netbox_interface_name_rules/tests/test_misc.py` | Remove `SupportModulePathTest` class and `UtilsModulePathFalseTest` class |
| `REUSE.toml` | `utils.py` will be deleted; no entry change needed |

No database migration is required. No API change.

---

## 2. Fix CSV export/import round-trip (KeyError `'Ch'`)

### Root cause

`InterfaceNameRuleTable.channel_start` has `verbose_name="Ch. Start"`. When the list view is exported, django-tables2 writes `"Ch. Start"` as the CSV column header. NetBox's `NetBoxModelImportForm.__init__` later normalises headers by calling something like `header.split('.')[0].strip()`, yielding `"Ch"` — a non-existent form field — and raises `KeyError`.

### Fix

Add `csv_headers` (class attribute) and `to_csv()` (instance method) to the `InterfaceNameRule` model following the standard NetBox pattern. When these are present, `ObjectListView.export_data()` uses them instead of the table's verbose_names, so export and import use identical field names.

Also rename `verbose_name="Ch. Start"` → `verbose_name="Channel Start"` in `tables.py` to avoid the ambiguity entirely.

```python
# models.py
csv_headers = [
    'module_type', 'module_type_pattern', 'module_type_is_regex',
    'parent_module_type', 'device_type', 'platform',
    'name_template', 'channel_count', 'channel_start',
    'description', 'enabled', 'applies_to_device_interfaces',
]

def to_csv(self):
    return (
        self.module_type.model if self.module_type else '',
        self.module_type_pattern,
        self.module_type_is_regex,
        self.parent_module_type.model if self.parent_module_type else '',
        self.device_type.model if self.device_type else '',
        self.platform.name if self.platform else '',
        self.name_template,
        self.channel_count,
        self.channel_start,
        self.description,
        self.enabled,
        self.applies_to_device_interfaces,
    )
```

The `csv_headers` field names match exactly the `InterfaceNameRuleImportForm.Meta.fields` list, so a round-trip is guaranteed.

---

## 3. Add YAML export

### YAML format

Matches the `contrib/` vendor files format — a YAML sequence of rule dicts using the same field names as the import form:

```yaml
- module_type: QSFP-100G-LR4
  module_type_pattern: ''
  module_type_is_regex: false
  parent_module_type: ''
  device_type: ACX7024
  platform: ''
  name_template: 'et-0/0/{bay_position}'
  channel_count: 0
  channel_start: 0
  description: ''
  enabled: true
  applies_to_device_interfaces: false
```

Empty FK fields are written as `''` (empty string) which matches what the import form treats as "not set".

### Export modes

| Mode | URL | Method | Behaviour |
|------|-----|--------|-----------|
| Export all | `/rules/export/yaml/` | GET | Downloads a single YAML file with every rule in pk order |
| Export selected | `/rules/export/yaml/` | POST | Body contains `pk_<n>` checkbox fields; returns YAML of those rules |

Using the same URL with GET vs POST allows both modes with one view.

### New view

```python
class InterfaceNameRuleYAMLExportView(ConditionalLoginRequiredMixin, View):
    """GET = all rules; POST = selected rules (pk_<n> checkboxes)."""
```

Returns a `HttpResponse` with `Content-Type: application/x-yaml` and `Content-Disposition: attachment; filename="interface_name_rules.yaml"`.

Uses `yaml.dump()` (PyYAML, already in NetBox's dependency tree).

### UI changes

- `interfacenamerule_list.html`: Add **"Export YAML"** link in the existing export/action area (same row as the existing "Export" CSV button). When rows are selected (checkbox state), clicking it POSTs the checked PKs; otherwise it GETs all.
- URL entry: `path("export/yaml/", ..., name="interfacenamerule_export_yaml")`.

---

## 4. Tests (TDD)

All tests must be written **before** the implementation code they exercise. Test files follow the existing convention (Django `TestCase`).

### Tests to add to `test_views.py`

| Test class | Test method | What it verifies |
|---|---|---|
| `BulkImportViewTest` | `test_import_csv_roundtrip` | POST a CSV generated by `model.to_csv()` / `csv_headers` to the import URL, assert no errors and new rule created |
| `BulkImportViewTest` | `test_import_csv_channel_start_field` | Specifically exercises `channel_start` column (regression for KeyError 'Ch') |
| `BulkImportViewTest` | `test_import_yaml_roundtrip` | POST a YAML payload to the import URL, assert no errors |
| `YAMLExportViewTest` | `test_export_all_yaml_get` | GET `/export/yaml/` returns 200 with YAML content-type and all rules |
| `YAMLExportViewTest` | `test_export_selected_yaml_post` | POST with selected PKs returns YAML with only those rules |
| `YAMLExportViewTest` | `test_export_yaml_roundtrip` | Export YAML, parse it, POST it back to import, assert rules created |
| `YAMLExportViewTest` | `test_export_yaml_empty` | GET when no rules exist returns an empty YAML list `[]\n` |

### Tests to add to `test_misc.py`

| Test class | Test method | What it verifies |
|---|---|---|
| `ModelCSVExportTest` | `test_csv_headers_match_import_form` | `InterfaceNameRule.csv_headers` == `InterfaceNameRuleImportForm.Meta.fields` |
| `ModelCSVExportTest` | `test_to_csv_output_count` | `to_csv()` returns a tuple with same length as `csv_headers` |
| `ModelCSVExportTest` | `test_to_csv_fk_field_natural_key` | `to_csv()` writes `module_type.model`, not a PK integer |

### Remove from `test_misc.py`

- `SupportModulePathTest`
- `UtilsModulePathFalseTest`

---

## 5. No migration needed

All changes are:
- Code / logic removal (module_path)
- Adding Python methods to the model (csv_headers / to_csv) — not ORM-level changes
- New view + URL
- Template changes
- Verbose_name fix in table

---

## Implementation order

1. Pull main, create branch
2. Remove module_path (no test impact except deletions)
3. **Write failing tests** for CSV round-trip and KeyError regression
4. Implement `csv_headers` + `to_csv()` on model, fix verbose_name
5. **Write failing tests** for YAML export
6. Implement YAML export view, URL, UI
7. Run full test suite — ensure green
8. Run `ruff check . && ruff format --check .`
