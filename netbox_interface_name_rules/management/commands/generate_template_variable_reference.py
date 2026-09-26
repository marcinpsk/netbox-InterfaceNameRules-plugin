# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Regenerate template-variable reference regions."""

from django.core.management.base import BaseCommand, CommandError

from netbox_interface_name_rules.template_variable_reference import write_generated_regions


class Command(BaseCommand):
    """Regenerate every catalogue-backed template-variable reference."""

    help = "Regenerate template-variable reference regions from the catalogue."

    def handle(self, *args, **options):
        """Write each generated region and report what changed."""
        try:
            changed = write_generated_regions()
        except (FileNotFoundError, ValueError) as error:
            raise CommandError(str(error)) from error
        if changed:
            self.stdout.write("Updated " + ", ".join(changed))
        else:
            self.stdout.write("Template-variable references are current.")
