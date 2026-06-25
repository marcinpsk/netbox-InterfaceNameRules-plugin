# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Integration tests for rule matching and interface renaming.

These tests create real DB objects and exercise the full engine pipeline.
"""

from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    Manufacturer,
    Module,
    ModuleBay,
    ModuleBayTemplate,
    ModuleType,
    Platform,
    Site,
)
from django.test import TestCase

from netbox_interface_name_rules.engine import (
    apply_interface_name_rules,
    build_variables,
    find_matching_rule,
)
from netbox_interface_name_rules.models import InterfaceNameRule


class FindMatchingRuleTest(TestCase):
    """Test rule lookup priority ordering."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="TestMfg", slug="testmfg")
        cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model="TEST-SFP", part_number="TEST-SFP")
        cls.parent_module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="TEST-CONVERTER", part_number="TEST-CONVERTER"
        )
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="TEST-DEVICE", slug="test-device")
        cls.platform = Platform.objects.create(name="TEST-OS", slug="test-os")

    def test_no_rules_returns_none(self):
        result = find_matching_rule(self.module_type, None, None)
        self.assertIsNone(result)

    def test_universal_rule_matches(self):
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="port{bay_position}",
        )
        result = find_matching_rule(self.module_type, None, None)
        self.assertEqual(result, rule)

    def test_device_specific_rule_preferred(self):
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="generic{bay_position}",
        )
        device_specific = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            device_type=self.device_type,
            name_template="specific{bay_position}",
        )
        result = find_matching_rule(self.module_type, None, self.device_type)
        self.assertEqual(result, device_specific)

    def test_parent_specific_rule_preferred(self):
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="generic{bay_position}",
        )
        parent_specific = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            parent_module_type=self.parent_module_type,
            name_template="parent{bay_position}",
        )
        result = find_matching_rule(self.module_type, self.parent_module_type, None)
        self.assertEqual(result, parent_specific)

    def test_full_match_most_specific(self):
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="generic{bay_position}",
        )
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            device_type=self.device_type,
            name_template="device{bay_position}",
        )
        full = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            parent_module_type=self.parent_module_type,
            device_type=self.device_type,
            name_template="full{bay_position}",
        )
        result = find_matching_rule(self.module_type, self.parent_module_type, self.device_type)
        self.assertEqual(result, full)

    def test_platform_specific_rule_preferred(self):
        """Platform-scoped rule is preferred over unscoped rule."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="generic{bay_position}",
        )
        platform_specific = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            platform=self.platform,
            name_template="platform{bay_position}",
        )
        result = find_matching_rule(self.module_type, None, None, platform=self.platform)
        self.assertEqual(result, platform_specific)

    def test_disabled_rule_not_returned(self):
        """Disabled exact rule is never returned even if it matches."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="disabled{bay_position}",
            enabled=False,
        )
        result = find_matching_rule(self.module_type, None, None)
        self.assertIsNone(result)

    def test_platform_none_does_not_match_platform_scoped_rule(self):
        """With platform=None, a platform-scoped rule is NOT matched."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            platform=self.platform,
            name_template="platform-only{bay_position}",
        )
        result = find_matching_rule(self.module_type, None, None, platform=None)
        self.assertIsNone(result)

    def test_platform_none_falls_back_to_unscoped_rule(self):
        """With platform=None, an unscoped rule is preferred over a platform-scoped one."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            platform=self.platform,
            name_template="platform{bay_position}",
        )
        unscoped = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="generic{bay_position}",
        )
        result = find_matching_rule(self.module_type, None, None, platform=None)
        self.assertEqual(result, unscoped)


class BuildVariablesTest(TestCase):
    """Test build_variables from module bay context."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="VarMfg", slug="varmfg")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="VAR-DEV", slug="var-dev")
        # Templates must be created BEFORE devices (instantiated on device creation)
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="Transceiver 5", position="5")
        role = DeviceRole.objects.create(name="VarRole", slug="varrole")
        site = Site.objects.create(name="VarSite", slug="varsite")
        cls.device = Device.objects.create(name="var-test-01", device_type=cls.device_type, role=role, site=site)

    def test_simple_bay_variables(self):
        bay = ModuleBay.objects.get(device=self.device, name="Transceiver 5")
        variables = build_variables(bay)
        self.assertEqual(variables["bay_position"], "5")
        self.assertEqual(variables["bay_position_num"], "5")
        self.assertEqual(variables["slot"], "5")

    def test_non_numeric_position(self):
        """Bay position with text prefix (e.g., 'swp1')."""
        manufacturer = Manufacturer.objects.create(name="NNMfg", slug="nnmfg")
        dt = DeviceType.objects.create(manufacturer=manufacturer, model="NN-DEV", slug="nn-dev")
        ModuleBayTemplate.objects.create(device_type=dt, name="Transceiver swp3", position="swp3")
        role = DeviceRole.objects.create(name="NNRole", slug="nnrole")
        site = Site.objects.create(name="NNSite", slug="nnsite")
        device = Device.objects.create(name="nn-test-01", device_type=dt, role=role, site=site)
        bay = ModuleBay.objects.get(device=device, name="Transceiver swp3")
        variables = build_variables(bay)
        self.assertEqual(variables["bay_position"], "swp3")
        self.assertEqual(variables["bay_position_num"], "3")


class ApplyInterfaceNameRulesTest(TestCase):
    """Test full apply_interface_name_rules pipeline."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="ApplyMfg", slug="applymfg")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="APPLY-DEV", slug="apply-dev")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="APPLY-SFP", part_number="APPLY-SFP"
        )
        # Templates before device
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="Transceiver 0", position="0")
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="Transceiver 1", position="1")
        role = DeviceRole.objects.create(name="ApplyRole", slug="applyrole")
        site = Site.objects.create(name="ApplySite", slug="applysite")
        cls.device = Device.objects.create(name="apply-test-01", device_type=cls.device_type, role=role, site=site)

    def test_simple_rename(self):
        """Module install with matching rule renames the interface."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            device_type=self.device_type,
            name_template="et-0/0/{bay_position}",
        )
        bay = ModuleBay.objects.get(device=self.device, name="Transceiver 0")
        module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.module_type)
        # Simulate what NetBox does: create an interface with the bay position as name
        iface = Interface.objects.create(device=self.device, module=module, name="0", type="10gbase-x-sfpp")
        renamed = apply_interface_name_rules(module, bay)
        self.assertEqual(renamed, 1)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "et-0/0/0")

    def test_no_matching_rule(self):
        """No rule exists — interfaces left untouched."""
        bay = ModuleBay.objects.get(device=self.device, name="Transceiver 1")
        module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="1", type="10gbase-x-sfpp")
        renamed = apply_interface_name_rules(module, bay)
        self.assertEqual(renamed, 0)

    def test_idempotency_guard(self):
        """Already-renamed interfaces are not re-processed."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            device_type=self.device_type,
            name_template="et-0/0/{bay_position}",
        )
        bay = ModuleBay.objects.get(device=self.device, name="Transceiver 0")
        module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.module_type)
        # Interface already has the final name (not the raw bay position)
        Interface.objects.create(device=self.device, module=module, name="et-0/0/0", type="10gbase-x-sfpp")
        renamed = apply_interface_name_rules(module, bay)
        self.assertEqual(renamed, 0)

    def test_breakout_creates_channels(self):
        """Breakout rule creates multiple channel interfaces."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            device_type=self.device_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            channel_count=4,
            channel_start=0,
        )
        bay = ModuleBay.objects.get(device=self.device, name="Transceiver 0")
        module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="0", type="10gbase-x-sfpp")
        renamed = apply_interface_name_rules(module, bay)
        self.assertEqual(renamed, 4)
        iface_names = sorted(Interface.objects.filter(module=module).values_list("name", flat=True))
        self.assertEqual(iface_names, ["xe-0/0/0:0", "xe-0/0/0:1", "xe-0/0/0:2", "xe-0/0/0:3"])

    def test_breakout_channel_start_offset(self):
        """Breakout with channel_start=1 (Cisco style)."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            device_type=self.device_type,
            name_template="HundredGigE0/0/0/{bay_position}/{channel}",
            channel_count=2,
            channel_start=1,
        )
        bay = ModuleBay.objects.get(device=self.device, name="Transceiver 0")
        module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="0", type="100gbase-x-qsfp28")
        renamed = apply_interface_name_rules(module, bay)
        self.assertEqual(renamed, 2)
        names = sorted(Interface.objects.filter(module=module).values_list("name", flat=True))
        self.assertEqual(names, ["HundredGigE0/0/0/0/1", "HundredGigE0/0/0/0/2"])

    def test_no_interfaces_returns_zero(self):
        """Module with no interfaces — nothing to rename."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            device_type=self.device_type,
            name_template="et-0/0/{bay_position}",
        )
        bay = ModuleBay.objects.get(device=self.device, name="Transceiver 1")
        module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.module_type)
        renamed = apply_interface_name_rules(module, bay)
        self.assertEqual(renamed, 0)


class FindMatchingRuleCachingTest(TestCase):
    """find_matching_rule loads rules once, memoizes per context, and reloads when rules change."""

    @classmethod
    def setUpTestData(cls):
        mfr = Manufacturer.objects.create(name="CacheMfg", slug="cachemfg")
        cls.module_type = ModuleType.objects.create(manufacturer=mfr, model="C-SFP", part_number="C-SFP")
        cls.device_type = DeviceType.objects.create(manufacturer=mfr, model="C-DEV", slug="c-dev")

    def setUp(self):
        # These tests mutate find_matching_rule()'s module-level _RULE_CACHE / _pin, which TestCase does
        # NOT roll back (only the DB is). Reset them before each method so the class is order-independent
        # and a stale snapshot from a prior method can't be reused for a same-content rule set.
        from netbox_interface_name_rules import engine

        engine._RULE_CACHE.update({"version": None, "exact": (), "regex": (), "memo": {}})
        engine._pin.depth = 0
        engine._pin.primed = False
        for attr in ("exact", "regex", "memo"):
            engine._pin.__dict__.pop(attr, None)

    def test_repeated_calls_do_not_re_query_rules(self):
        """A second identical call issues at most the cheap fingerprint query — no per-candidate lookups."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        InterfaceNameRule.objects.create(module_type=self.module_type, name_template="port{bay_position}")
        find_matching_rule(self.module_type, None, self.device_type)  # warm (rule create bumped the fingerprint)

        with CaptureQueriesContext(connection) as ctx:
            find_matching_rule(self.module_type, None, self.device_type)

        # No per-candidate rule lookups — at most the single content-hash fingerprint query.
        self.assertLessEqual(len(ctx), 1, [q["sql"] for q in ctx.captured_queries])

    def test_memoized_result_is_not_recomputed(self):
        """A repeat call with the same context returns the memoized rule without re-running tier matching."""
        from netbox_interface_name_rules import engine

        InterfaceNameRule.objects.create(module_type=self.module_type, name_template="port{bay_position}")
        first = find_matching_rule(self.module_type, None, self.device_type)  # warm, populates the memo

        calls = []
        real_find_exact = engine._find_exact_match

        def counting_find_exact(*args, **kwargs):
            calls.append(1)
            return real_find_exact(*args, **kwargs)

        engine._find_exact_match = counting_find_exact
        try:
            second = find_matching_rule(self.module_type, None, self.device_type)
        finally:
            engine._find_exact_match = real_find_exact

        self.assertEqual(second, first)
        self.assertEqual(calls, [], "second call must be served from the memo, not re-matched")

    def test_rule_change_invalidates_cache(self):
        """Adding a more specific rule changes the fingerprint, so the next call reloads and matches it."""
        find_matching_rule(self.module_type, None, self.device_type)  # warm with the current rule set

        specific = InterfaceNameRule.objects.create(
            module_type=self.module_type, device_type=self.device_type, name_template="specific{bay_position}"
        )
        result = find_matching_rule(self.module_type, None, self.device_type)

        self.assertEqual(result, specific)

    def test_save_edit_invalidates_cache(self):
        """A same-count .save() edit bumps last_updated, so the fingerprint changes and the cache reloads."""
        rule = InterfaceNameRule.objects.create(module_type=self.module_type, name_template="old{bay_position}")
        self.assertEqual(find_matching_rule(self.module_type, None, None).name_template, "old{bay_position}")

        rule.name_template = "new{bay_position}"
        rule.save()

        self.assertEqual(find_matching_rule(self.module_type, None, None).name_template, "new{bay_position}")

    def test_scope_fk_update_invalidates_cache(self):
        """An FK edit that bypasses last_updated (bulk .update()/SET_NULL cascade) still changes the fingerprint."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type, device_type=self.device_type, name_template="x{bay_position}"
        )
        cached = find_matching_rule(self.module_type, None, self.device_type)
        self.assertEqual(cached, rule)
        self.assertEqual(cached.device_type_id, self.device_type.pk)  # the device-scoped copy is cached

        # Null the scope FK the way a SET_NULL cascade / bulk .update() does: a straight UPDATE that
        # does NOT bump last_updated and leaves the enabled-rule count unchanged. The fingerprint's
        # device_type-id sum still changes, so the next call reloads instead of serving the stale copy.
        InterfaceNameRule.objects.filter(pk=rule.pk).update(device_type=None)

        refreshed = find_matching_rule(self.module_type, None, self.device_type)
        self.assertIsNone(
            refreshed.device_type_id,
            "served a stale device_type-scoped rule after an FK edit that bypassed last_updated",
        )

    def test_bulk_text_update_invalidates_cache(self):
        """A raw bulk .update() of name_template (no auto_now, same count/sums) still reloads the cache.

        This is the case a count/sum aggregate fingerprint cannot see: a direct column write that
        bumps neither last_updated nor any FK/channel sum. The content-hash fingerprint catches it,
        so the engine never serves a stale template.
        """
        rule = InterfaceNameRule.objects.create(module_type=self.module_type, name_template="old{bay_position}")
        self.assertEqual(find_matching_rule(self.module_type, None, None).name_template, "old{bay_position}")

        # Bulk .update() writes the column directly — no .save(), so auto_now/last_updated is NOT
        # bumped and the enabled-rule count and column sums are unchanged. Only the text differs.
        InterfaceNameRule.objects.filter(pk=rule.pk).update(name_template="new{bay_position}")

        self.assertEqual(
            find_matching_rule(self.module_type, None, None).name_template,
            "new{bay_position}",
            "served a stale name_template after a bulk .update() the count/sum fingerprint missed",
        )

    def test_bulk_pattern_update_invalidates_cache(self):
        """A raw bulk .update() of module_type_pattern (regex tier) also reloads the cache."""
        mfr = Manufacturer.objects.create(name="PatMfg", slug="patmfg")
        mt = ModuleType.objects.create(manufacturer=mfr, model="OLD-XCVR", part_number="OLD-XCVR")
        rule = InterfaceNameRule.objects.create(
            module_type_is_regex=True, module_type_pattern=r"OLD-.*", name_template="p{bay_position}"
        )
        self.assertEqual(find_matching_rule(mt, None, None), rule)  # OLD-XCVR matches OLD-.*

        InterfaceNameRule.objects.filter(pk=rule.pk).update(module_type_pattern=r"NEW-.*")

        self.assertIsNone(
            find_matching_rule(mt, None, None),
            "served a stale regex pattern after a bulk .update() the count/sum fingerprint missed",
        )

    def test_compensating_fk_swap_changes_fingerprint(self):
        """Swapping two rules' device_type keeps the FK-id SUM constant but must still change the version.

        A Sum()-based fingerprint collides here (the summed ids are identical after the swap, count
        and last_updated unchanged); the per-row, pk-anchored content hash distinguishes them.
        """
        from netbox_interface_name_rules import engine

        mfr = Manufacturer.objects.create(name="SwapMfg", slug="swapmfg")
        mt_a = ModuleType.objects.create(manufacturer=mfr, model="SWAP-A", part_number="SWAP-A")
        mt_b = ModuleType.objects.create(manufacturer=mfr, model="SWAP-B", part_number="SWAP-B")
        dt_x = DeviceType.objects.create(manufacturer=mfr, model="SWAP-X", slug="swap-x")
        dt_y = DeviceType.objects.create(manufacturer=mfr, model="SWAP-Y", slug="swap-y")
        rule_a = InterfaceNameRule.objects.create(module_type=mt_a, device_type=dt_x, name_template="a{bay_position}")
        rule_b = InterfaceNameRule.objects.create(module_type=mt_b, device_type=dt_y, name_template="b{bay_position}")

        before = engine._enabled_rules_version()
        # Swap device_type between the two rules via bulk .update(): SUM(device_type) is unchanged
        # (x+y == y+x), count unchanged, last_updated unbumped. Only the per-rule pairing differs.
        InterfaceNameRule.objects.filter(pk=rule_a.pk).update(device_type=dt_y)
        InterfaceNameRule.objects.filter(pk=rule_b.pk).update(device_type=dt_x)
        after = engine._enabled_rules_version()

        self.assertNotEqual(before, after, "fingerprint collided on a compensating FK swap (Sum-aggregate weakness)")

    def test_module_type_model_rename_reevaluated(self):
        """Renaming ModuleType.model re-evaluates regex rules — the memo is keyed on the live model name."""
        mfr = Manufacturer.objects.create(name="RenameMfg", slug="renamemfg")
        mt = ModuleType.objects.create(manufacturer=mfr, model="OLD-XCVR", part_number="OLD-XCVR")
        rule = InterfaceNameRule.objects.create(
            module_type_is_regex=True, module_type_pattern=r"NEW-.*", name_template="p{bay_position}"
        )

        self.assertIsNone(find_matching_rule(mt, None, None))  # "OLD-XCVR" does not match NEW-.*

        mt.model = "NEW-XCVR"
        mt.save()

        self.assertEqual(find_matching_rule(mt, None, None), rule)  # now matches; must not serve the stale miss

    def test_none_module_type_returns_none(self):
        """find_matching_rule(None, ...) returns None instead of raising — module rules need a module type."""
        self.assertIsNone(find_matching_rule(None, None, None))

        InterfaceNameRule.objects.create(module_type=self.module_type, name_template="p{bay_position}")
        self.assertIsNone(find_matching_rule(None, None, self.device_type))

    def test_find_exact_match_loads_rules_on_demand(self):
        """_find_exact_match loads the rule set itself when called without preloaded rules (direct-caller path)."""
        from netbox_interface_name_rules.engine import _find_exact_match

        rule = InterfaceNameRule.objects.create(module_type=self.module_type, name_template="p{bay_position}")
        # No exact_rules argument → the function loads them via _get_enabled_rules() rather than a passed-in set.
        self.assertEqual(_find_exact_match(self.module_type, [(None, None, None)]), rule)

    def test_memo_is_bounded(self):
        """The per-version memo is capped, so it can't grow without bound under a stable rule set."""
        from netbox_interface_name_rules import engine

        original_max = engine._MEMO_MAX
        engine._MEMO_MAX = 2
        try:
            mfr = Manufacturer.objects.create(name="MemoMfg", slug="memomfg")
            module_types = [
                ModuleType.objects.create(manufacturer=mfr, model=f"MEMO-{i}", part_number=f"MEMO-{i}")
                for i in range(5)
            ]
            for mt in module_types:  # five distinct contexts → five distinct memo keys
                find_matching_rule(mt, None, None)

            self.assertLessEqual(len(engine._RULE_CACHE["memo"]), engine._MEMO_MAX)
        finally:
            engine._MEMO_MAX = original_max

    def test_pinned_rule_cache_skips_per_call_fingerprint(self):
        """Inside pinned_rule_cache() the per-call fingerprint query is elided; outside it still runs."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_interface_name_rules.engine import pinned_rule_cache

        InterfaceNameRule.objects.create(module_type=self.module_type, name_template="p{bay_position}")
        find_matching_rule(self.module_type, None, self.device_type)  # warm + memoize this context

        # Outside the pin, every call (even a memoized repeat) issues the fingerprint aggregate.
        with CaptureQueriesContext(connection) as unpinned:
            for _ in range(5):
                find_matching_rule(self.module_type, None, self.device_type)
        self.assertEqual(len(unpinned), 5, [q["sql"] for q in unpinned.captured_queries])

        # Inside the pin, the first lookup primes the set (one query) and the rest reuse it: a loop of
        # many lookups costs a single fingerprint query instead of one per call.
        with pinned_rule_cache(), CaptureQueriesContext(connection) as pinned:
            for _ in range(5):
                find_matching_rule(self.module_type, None, self.device_type)
        self.assertEqual(len(pinned), 1, [q["sql"] for q in pinned.captured_queries])

    def test_pinned_block_uses_set_loaded_at_entry_then_resumes(self):
        """A pinned block matches against the set loaded at entry; normal self-invalidation resumes after."""
        from netbox_interface_name_rules.engine import pinned_rule_cache

        universal = InterfaceNameRule.objects.create(module_type=self.module_type, name_template="a{bay_position}")

        with pinned_rule_cache():
            self.assertEqual(find_matching_rule(self.module_type, None, self.device_type), universal)
            # A more specific rule added inside the block is intentionally not observed until exit.
            specific = InterfaceNameRule.objects.create(
                module_type=self.module_type, device_type=self.device_type, name_template="b{bay_position}"
            )
            self.assertEqual(find_matching_rule(self.module_type, None, self.device_type), universal)

        # After the block, the fingerprint is re-read and the more specific rule wins.
        self.assertEqual(find_matching_rule(self.module_type, None, self.device_type), specific)

    def test_pinned_block_holds_snapshot_across_concurrent_cache_reload(self):
        """A pinned batch keeps its rule-set snapshot even if another thread reloads the shared cache.

        The pin must capture exact/regex/memo at prime time; if it re-read the live module-level
        _RULE_CACHE, a concurrent request reloading it mid-block would silently switch this batch to a
        different rule set — renaming one device's modules with mixed rule versions.
        """
        from netbox_interface_name_rules import engine
        from netbox_interface_name_rules.engine import pinned_rule_cache

        universal = InterfaceNameRule.objects.create(module_type=self.module_type, name_template="a{bay_position}")

        with pinned_rule_cache():
            self.assertEqual(find_matching_rule(self.module_type, None, self.device_type), universal)  # primes

            # Simulate a concurrent request on another thread reloading _RULE_CACHE to a different
            # version mid-block — exactly what _get_enabled_rules() does on a version change.
            engine._RULE_CACHE["version"] = "concurrent-reload-other-version"
            engine._RULE_CACHE["exact"] = ()
            engine._RULE_CACHE["regex"] = ()
            engine._RULE_CACHE["memo"] = {}

            # Still inside the pin: must serve the snapshot captured at entry, not the now-empty global.
            self.assertEqual(
                find_matching_rule(self.module_type, None, self.device_type),
                universal,
                "pinned block switched rule sets after a concurrent _RULE_CACHE reload",
            )
