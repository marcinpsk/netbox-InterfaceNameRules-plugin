# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for complete template claim relations."""

from django.test import SimpleTestCase

from netbox_interface_name_rules.family.claims import TemplateClaim, resolve_template_claims


def _two_names(template_name, names):
    return (
        f"Interface template {template_name!r} of module could name any of {names} as its raw name or its "
        "renamed form; skipping them all rather than renaming a guess."
    )


def _two_templates(name, template_names):
    return (
        f"Interface {name!r} on module could be the raw or renamed name of any of the templates "
        f"{template_names}; skipping it rather than renaming a guess."
    )


class TemplateClaimsTest(SimpleTestCase):
    def test_claim_copies_units_from_mutable_lists(self):
        unit = ["x"]
        units = [unit]
        claim = TemplateClaim(1, "A", units)
        self.assertEqual(claim.units, (("x",),))
        unit.append("y")
        units.append(["z"])
        self.assertEqual(claim.units, (("x",),))

    def test_empty_relation(self):
        self.assertEqual(resolve_template_claims((), module="module"), ((), ()))

    def test_duplicate_units_count_once_and_keep_claimant_order(self):
        claims = (
            TemplateClaim(9, "A", (("z",), ("z",))),
            TemplateClaim(2, "B", ()),
            TemplateClaim(1, "C", (("x",),)),
        )
        self.assertEqual(resolve_template_claims(iter(claims), module="module"), (((9, ("z",)), (1, ("x",))), ()))

    def test_claimant_with_two_units_is_rejected(self):
        claims = (TemplateClaim(1, "A", (("y",), ("x",), ("y",))),)
        self.assertEqual(
            resolve_template_claims(claims, module="module"),
            ((), (_two_names("A", ["x", "y"]),)),
        )

    def test_a_unit_that_holds_every_other_unit_is_one_claim(self):
        claims = (TemplateClaim(1, "A", (("x:0",), ("x:0", "x:1"))),)
        self.assertEqual(resolve_template_claims(claims, module="module"), (((1, ("x:0", "x:1")),), ()))

    def test_a_family_and_a_name_outside_it_are_two_claims(self):
        claims = (TemplateClaim(1, "A", (("x:0", "x:1"), ("y",))),)
        self.assertEqual(
            resolve_template_claims(claims, module="module"),
            ((), (_two_names("A", ["x:0", "x:1", "y"]),)),
        )

    def test_two_units_that_hold_the_same_names_are_two_claims(self):
        claims = (TemplateClaim(1, "A", (("x:0", "x:1"), ("x:1", "x:0"))),)
        self.assertEqual(
            resolve_template_claims(claims, module="module"),
            ((), (_two_names("A", ["x:0", "x:1"]),)),
        )

    def test_name_with_two_claimants_is_rejected(self):
        claims = (TemplateClaim(1, "B", (("x", "x"),)), TemplateClaim(2, "A", (("x",),)))
        self.assertEqual(
            resolve_template_claims(claims, module="module"),
            ((), (_two_templates("x", ["A", "B"]),)),
        )

    def test_a_name_two_families_share_rejects_both(self):
        claims = (TemplateClaim(1, "A", (("x:0", "x:1"),)), TemplateClaim(2, "B", (("x:1", "y"),)))
        self.assertEqual(
            resolve_template_claims(claims, module="module"),
            ((), (_two_templates("x:1", ["A", "B"]),)),
        )

    def test_complete_mixed_relation_rejects_shared_names(self):
        claims = (
            TemplateClaim(1, "A", (("x",), ("y",))),
            TemplateClaim(2, "B", (("y",),)),
            TemplateClaim(3, "C", (("z",),)),
        )
        self.assertEqual(
            resolve_template_claims(claims, module="module"),
            (((3, ("z",)),), (_two_names("A", ["x", "y"]), _two_templates("y", ["A", "B"]))),
        )

    def test_duplicate_claimant_id_raises(self):
        claims = (TemplateClaim(1, "A", ()), TemplateClaim(1, "B", (("x",),)))
        with self.assertRaisesRegex(ValueError, "Duplicate claimant_id"):
            resolve_template_claims(claims, module="module")

    def test_claim_is_immutable(self):
        from dataclasses import FrozenInstanceError

        claim = TemplateClaim(1, "A", (("x",),))
        with self.assertRaises(FrozenInstanceError):
            claim.claimant_id = 2

    def test_primitive_does_not_log_collisions(self):
        claims = (TemplateClaim(1, "A", (("x",), ("y",))),)
        with self.assertNoLogs("netbox_interface_name_rules", level="WARNING"):
            accepted, messages = resolve_template_claims(claims, module="module")
        self.assertEqual(accepted, ())
        self.assertEqual(len(messages), 1)
