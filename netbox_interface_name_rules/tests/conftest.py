# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Pytest fixtures for isolated parallel test workers."""

import functools
import os

import pytest

from netbox_interface_name_rules.tests.parallel import isolated_test_database_name


@pytest.fixture(scope="session")
def django_db_modify_db_settings(django_db_modify_db_settings):
    """Give each pytest worker a private PostgreSQL database."""
    from django.conf import settings

    test_settings = dict(settings.DATABASES["default"].get("TEST") or {})
    test_settings["NAME"] = isolated_test_database_name(
        os.environ["TEST_DB_NAME"],
        os.environ.get("PYTEST_XDIST_WORKER"),
    )
    settings.DATABASES["default"]["TEST"] = test_settings


@functools.lru_cache(maxsize=1)
def _preview_key_contract():
    """Return the rule-test form's fields and the variable names its preview derives.

    `build_variables` is called without a device, which is the set `RuleTestView` rebuilds; it adds
    `base` always and `channel` when `channel_count` is positive. `vc_position` is deliberately not
    in it: the preview never derives it and it is a live NetBox device form key.
    """
    from dcim.models import ModuleBay

    from netbox_interface_name_rules.forms import RuleTestForm
    from netbox_interface_name_rules.naming import build_variables

    fields = frozenset(RuleTestForm().fields)
    return fields, frozenset(build_variables(ModuleBay())) | frozenset({"base", "channel"})


def dropped_preview_keys(data):
    """Return the keys of *data* that name a preview variable the rule-test form has no field for."""
    fields, variables = _preview_key_contract()
    return sorted(key for key in data if key not in fields and (key.startswith("var_") or key in variables))


def refuse_dropped_preview_keys(data):
    """Raise when *data* carries a preview variable the rule-test form would drop."""
    dropped = dropped_preview_keys(data)
    if dropped:
        keys = ", ".join(repr(key) for key in dropped)
        raise AssertionError(
            f"POST carries {keys}, which RuleTestForm does not declare. Django drops the key, so the "
            f"preview falls back to the field default and the assertion passes without reading the "
            f"submitted value. Submit a position the preview derives this value from."
        )


@pytest.fixture(autouse=True)
def _refuse_dropped_preview_keys(monkeypatch):
    """Fail any request that reaches the rule tester with a preview variable the form would drop."""
    from netbox_interface_name_rules.views import RuleTestView

    original = RuleTestView.post

    @functools.wraps(original)
    def post(self, request):
        refuse_dropped_preview_keys(request.POST)
        return original(self, request)

    monkeypatch.setattr(RuleTestView, "post", post)
