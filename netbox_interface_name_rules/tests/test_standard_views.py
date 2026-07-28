# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

"""NetBox's own view/API test mixins applied to InterfaceNameRule.

These cover the standard object views and REST endpoints — including object-level
permission constraints, the changelog view and the bulk endpoints — and they track
whatever the installed NetBox expects, so a convention change upstream surfaces here.
Behaviour unique to this plugin (test/apply/toggle/duplicate) is covered in test_views.py.
"""

from dcim.models import DeviceType, Manufacturer, ModuleType, Platform
from django.test import SimpleTestCase
from utilities.testing import APIViewTestCases, ViewTestCases

from netbox_interface_name_rules.models import InterfaceNameRule

BASE_URL = "plugins:netbox_interface_name_rules:interfacenamerule_{}"


def _create_dcim_fixtures():
    """Build the DCIM objects the rules reference, returning them for both test cases."""
    manufacturer = Manufacturer.objects.create(name="Test Manufacturer", slug="test-manufacturer")
    module_types = ModuleType.objects.bulk_create(
        [ModuleType(manufacturer=manufacturer, model=f"TEST-MT-{i}") for i in range(1, 7)]
    )
    device_type = DeviceType.objects.create(manufacturer=manufacturer, model="TEST-DT-1", slug="test-dt-1")
    platform = Platform.objects.create(name="Test Platform", slug="test-platform")
    return module_types, device_type, platform


class GraphQLFilterRegistrationTest(SimpleTestCase):
    """Guards against the GraphQL filter silently disabling itself.

    filters.py degrades to no filter when its imports fail, which is correct on NetBox 4.3
    but hides a real breakage anywhere else — strawberry_django renamed FilterLookup to
    StrFilterLookup in 0.86, and importing the wrong one just skipped the filter on 4.5.
    """

    @staticmethod
    def _netbox_provides_filter_base():
        """True on NetBox 4.5+. Only a genuinely absent module counts — anything else raises."""
        try:
            import netbox.graphql.filters  # noqa: F401
        except ModuleNotFoundError as exc:
            missing = exc.name or ""
            if missing == "netbox.graphql.filters" or "netbox.graphql.filters".startswith(f"{missing}."):
                return False
            raise
        return True

    def test_filter_is_registered_when_netbox_provides_the_base(self):
        if not self._netbox_provides_filter_base():
            self.skipTest("NetBox 4.3 has no shared GraphQL filter base")

        from netbox_interface_name_rules.graphql.filters import InterfaceNameRuleFilter

        self.assertIsNotNone(
            InterfaceNameRuleFilter,
            "GraphQL filter failed to import on a NetBox version that supports filters",
        )

    def test_unexpected_import_error_is_not_swallowed(self):
        """A renamed strawberry_django symbol must raise, not quietly drop the filter.

        Loads a fresh copy of filters.py with strawberry_django replaced by a module that
        lacks BaseFilterLookup. The decorator never runs, so nothing is registered twice.
        """
        if not self._netbox_provides_filter_base():
            self.skipTest("NetBox 4.3 legitimately has no filter base")

        import importlib.util
        import sys
        import types
        from unittest import mock

        from netbox_interface_name_rules.graphql import filters as real_filters

        spec = importlib.util.spec_from_file_location("_inr_filters_probe", real_filters.__file__)
        probe = importlib.util.module_from_spec(spec)
        stand_in = types.ModuleType("strawberry_django")  # no BaseFilterLookup on it

        with mock.patch.dict(sys.modules, {"strawberry_django": stand_in}):
            with self.assertRaises(ImportError):
                spec.loader.exec_module(probe)

    def test_legacy_alias_is_used_only_for_a_genuinely_absent_symbol(self):
        """An ImportError while resolving StrFilterLookup must not silently pick FilterLookup.

        Only the symbol being absent means "old strawberry-graphql-django". Anything else is a
        real failure, and quietly falling back to the legacy alias would hide it.
        """
        if not self._netbox_provides_filter_base():
            self.skipTest("NetBox 4.3 legitimately has no filter base")

        import importlib.util
        import sys
        import types
        from unittest import mock

        import strawberry_django as real_strawberry_django

        from netbox_interface_name_rules.graphql import filters as real_filters

        class _FailsOnStrFilterLookup(types.ModuleType):
            def __getattr__(self, name):
                if name == "StrFilterLookup":
                    raise ImportError("simulated failure inside strawberry_django")
                return getattr(real_strawberry_django, name)

        spec = importlib.util.spec_from_file_location("_inr_filters_probe_alias", real_filters.__file__)
        probe = importlib.util.module_from_spec(spec)

        with mock.patch.dict(sys.modules, {"strawberry_django": _FailsOnStrFilterLookup("strawberry_django")}):
            with self.assertRaises(ImportError):
                spec.loader.exec_module(probe)

    def test_type_declares_the_filter_when_available(self):
        from netbox_interface_name_rules.graphql.filters import InterfaceNameRuleFilter
        from netbox_interface_name_rules.graphql.types import _type_kwargs

        if InterfaceNameRuleFilter is None:
            self.skipTest("No GraphQL filter base on this NetBox version")
        self.assertIs(_type_kwargs.get("filters"), InterfaceNameRuleFilter)


class InterfaceNameRuleViewTestCase(ViewTestCases.PrimaryObjectViewTestCase):
    """Standard object views: get, list, create, edit, delete, changelog, bulk import/edit/delete."""

    model = InterfaceNameRule

    def _get_base_url(self):
        # Plugin views live under the "plugins" namespace, which the default does not know about.
        return BASE_URL

    def test_export_objects(self):
        """The list view exports YAML rather than CSV, so assert that instead of NetBox's default."""
        self.add_permissions("netbox_interface_name_rules.view_interfacenamerule")
        response = self.client.get(f"{self._get_url('list')}?export")
        self.assertHttpStatus(response, 200)
        self.assertEqual(response.get("Content-Type"), "text/yaml")

    @classmethod
    def setUpTestData(cls):
        module_types, device_type, platform = _create_dcim_fixtures()

        InterfaceNameRule.objects.bulk_create(
            [
                InterfaceNameRule(module_type=module_types[0], name_template="Ethernet{slot}/1"),
                InterfaceNameRule(module_type=module_types[1], name_template="Ethernet{slot}/2"),
                InterfaceNameRule(module_type=module_types[2], name_template="Ethernet{slot}/3"),
            ]
        )

        cls.form_data = {
            "module_type": module_types[3].pk,
            "module_type_pattern": "",
            "module_type_is_regex": False,
            "parent_module_type": None,
            "device_type": device_type.pk,
            "platform": platform.pk,
            "name_template": "GigabitEthernet{slot}/{channel}",
            "channel_count": 4,
            "channel_start": 1,
            "description": "Created by the standard view test",
            "enabled": True,
            "applies_to_device_interfaces": False,
        }

        # module_type is matched by model name (to_field_name="model") on import.
        cls.csv_data = (
            "module_type,module_type_pattern,module_type_is_regex,name_template,channel_count,channel_start,enabled",
            f"{module_types[4].model},,false,Ethernet{{slot}}/10,0,0,true",
            f"{module_types[5].model},,false,Ethernet{{slot}}/11,0,0,true",
            ",QSFP-DD-400G-.*,true,Ethernet{slot}/12,4,1,true",
        )

        cls.csv_update_data = (
            "id,name_template,description",
            f"{InterfaceNameRule.objects.first().pk},Ethernet{{slot}}/99,Updated via CSV import",
        )

        cls.bulk_edit_data = {
            "description": "Bulk edited",
            "enabled": False,
        }


class InterfaceNameRuleAPIViewTestCase(APIViewTestCases.APIViewTestCase):
    """REST API: get, list, create, update, delete, their bulk forms, OPTIONS, brief mode and GraphQL."""

    model = InterfaceNameRule
    brief_fields = ["description", "display", "id", "name_template", "url"]
    # Plugin API routes are namespaced under plugins-api, not the bare app label.
    view_namespace = "plugins-api:netbox_interface_name_rules"

    @classmethod
    def setUpTestData(cls):
        module_types, device_type, platform = _create_dcim_fixtures()

        InterfaceNameRule.objects.bulk_create(
            [
                InterfaceNameRule(module_type=module_types[0], name_template="Ethernet{slot}/1"),
                InterfaceNameRule(module_type=module_types[1], name_template="Ethernet{slot}/2"),
                InterfaceNameRule(module_type=module_types[2], name_template="Ethernet{slot}/3"),
            ]
        )

        cls.create_data = [
            {
                "module_type": module_types[3].pk,
                "name_template": "Ethernet{slot}/4",
            },
            {
                "module_type": module_types[4].pk,
                "name_template": "Ethernet{slot}/5",
                "device_type": device_type.pk,
            },
            {
                "module_type": module_types[5].pk,
                "name_template": "Ethernet{slot}/6",
                "platform": platform.pk,
            },
        ]

        cls.bulk_update_data = {
            "description": "Bulk updated over the API",
            "enabled": False,
        }
