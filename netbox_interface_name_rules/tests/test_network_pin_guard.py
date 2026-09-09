# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Exercise the network pin guard without Docker."""

import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.test import SimpleTestCase

_SCRIPT = Path(__file__).resolve().parents[2] / ".devcontainer/scripts/tests/test-network-pins.sh"


@skipUnless(shutil.which("bash"), "bash is required")
class NetworkPinGuardTest(SimpleTestCase):
    def _run(self, config):
        with TemporaryDirectory() as directory:
            fixture = Path(directory) / "config.yaml"
            fixture.write_text(config, encoding="utf-8")
            return subprocess.run(
                ["bash", str(_SCRIPT)],
                env={**os.environ, "NETWORK_PINS_CONFIG": str(fixture)},
                capture_output=True,
                text=True,
                check=False,
            )

    def test_pins_outside_one_range_are_accepted(self):
        result = self._run('ip_range: "198.18.0.128/25"\nipv4_address: "198.18.0.2"\n')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_pin_inside_the_range_is_rejected(self):
        result = self._run("ip_range: 198.18.0.128/25\nipv4_address: 198.18.0.129\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lies inside the dynamic range", result.stderr)

    def test_multiple_ranges_are_rejected(self):
        result = self._run("ip_range: 198.18.0.128/25\nip_range: 198.18.1.128/25\nipv4_address: 198.18.0.2\n")
        self.assertNotEqual(result.returncode, 0)

    def test_a_range_with_host_bits_is_rejected(self):
        result = self._run("ip_range: 198.18.0.129/25\nipv4_address: 198.18.0.2\n")
        self.assertNotEqual(result.returncode, 0)

    def test_a_missing_range_is_rejected(self):
        result = self._run("ipv4_address: 198.18.0.2\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no ip_range", result.stderr)

    def test_missing_pins_are_rejected(self):
        result = self._run("ip_range: 198.18.0.128/25\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no service pins", result.stderr)

    def test_a_quote_in_an_address_is_rejected(self):
        result = self._run("ip_range: 198.18.0.128/25\nipv4_address: 198.18.0.2'\n")
        self.assertNotEqual(result.returncode, 0)
