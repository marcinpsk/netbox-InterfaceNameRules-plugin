# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Pytest fixtures for isolated parallel test workers."""

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
