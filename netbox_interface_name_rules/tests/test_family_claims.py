# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for complete template claim relations."""

from django.test import SimpleTestCase

from netbox_interface_name_rules import family


class TemplateClaimsTest(SimpleTestCase):
    def test_claim_copies_labels_from_a_mutable_list(self):
        labels = ["x"]
        claim = family.TemplateClaim(1, "A", labels)
        self.assertIsInstance(claim.labels, tuple)
        labels.append("y")
        self.assertEqual(claim.labels, ("x",))

    def test_empty_relation(self):
        self.assertEqual(
            family.resolve_template_claims((), module="module", label_kind="interface name"),
            ((), ()),
        )

    def test_duplicate_edges_count_once_and_keep_claimant_order(self):
        claims = (
            family.TemplateClaim(9, "A", ("z", "z")),
            family.TemplateClaim(2, "B", ()),
            family.TemplateClaim(1, "C", ("x",)),
        )
        self.assertEqual(
            family.resolve_template_claims(iter(claims), module="module", label_kind="interface name"),
            (((9, "z"), (1, "x")), ()),
        )

    def test_claimant_with_two_labels_is_rejected(self):
        claims = (family.TemplateClaim(1, "A", ("y", "x", "y")),)
        self.assertEqual(
            family.resolve_template_claims(claims, module="module", label_kind="interface name"),
            (
                (),
                (
                    (
                        "Interface template 'A' of module could name any of ['x', 'y'] since this device's "
                        "virtual-chassis position changed; skipping them all rather than renaming a guess."
                    ),
                ),
            ),
        )

    def test_label_with_two_claimants_is_rejected(self):
        claims = (family.TemplateClaim(1, "B", ("x", "x")), family.TemplateClaim(2, "A", ("x",)))
        for label_kind, subject in (("interface name", "Interface"), ("family base", "Family base")):
            with self.subTest(label_kind=label_kind):
                self.assertEqual(
                    family.resolve_template_claims(claims, module="module", label_kind=label_kind),
                    (
                        (),
                        (
                            (
                                f"{subject} 'x' on module could be the drifted name of any of the templates "
                                "['A', 'B']; skipping it rather than renaming a guess."
                            ),
                        ),
                    ),
                )

    def test_complete_mixed_relation_rejects_shared_label(self):
        claims = (
            family.TemplateClaim(1, "A", ("x", "y")),
            family.TemplateClaim(2, "B", ("y",)),
            family.TemplateClaim(3, "C", ("z",)),
        )
        self.assertEqual(
            family.resolve_template_claims(claims, module="module", label_kind="family base"),
            (
                ((3, "z"),),
                (
                    (
                        "Interface template 'A' of module could name any of ['x', 'y'] since this device's "
                        "virtual-chassis position changed; skipping them all rather than renaming a guess."
                    ),
                    (
                        "Family base 'y' on module could be the drifted name of any of the templates "
                        "['A', 'B']; skipping it rather than renaming a guess."
                    ),
                ),
            ),
        )

    def test_duplicate_claimant_id_raises(self):
        claims = (family.TemplateClaim(1, "A", ()), family.TemplateClaim(1, "B", ("x",)))
        with self.assertRaisesRegex(ValueError, "Duplicate claimant_id"):
            family.resolve_template_claims(claims, module="module", label_kind="interface name")

    def test_invalid_label_kind_raises(self):
        with self.assertRaisesRegex(ValueError, "label_kind"):
            family.resolve_template_claims((), module="module", label_kind="port")

    def test_claim_is_immutable(self):
        from dataclasses import FrozenInstanceError

        claim = family.TemplateClaim(1, "A", ("x",))
        with self.assertRaises(FrozenInstanceError):
            claim.claimant_id = 2

    def test_primitive_does_not_log_collisions(self):
        claims = (family.TemplateClaim(1, "A", ("x", "y")),)
        with self.assertNoLogs("netbox_interface_name_rules", level="WARNING"):
            accepted, messages = family.resolve_template_claims(claims, module="module", label_kind="interface name")
        self.assertEqual(accepted, ())
        self.assertEqual(len(messages), 1)
