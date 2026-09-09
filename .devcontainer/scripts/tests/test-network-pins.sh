#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/.devcontainer/docker-compose.yml"

if [ -n "${NETWORK_PINS_CONFIG:-}" ]; then
  config="$(cat -- "$NETWORK_PINS_CONFIG")"
else
  config="$(docker compose -f "$COMPOSE_FILE" config)"
fi

mapfile -t ranges < <(sed -n 's/^[[:space:]]*ip_range:[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' <<< "$config")
if [ "${#ranges[@]}" -eq 0 ]; then
  echo "FAIL: the default network declares no ip_range, so Docker may hand a pinned address to another service" >&2
  exit 1
fi

if [ "${#ranges[@]}" -gt 1 ]; then
  echo "FAIL: expected one ip_range, found ${#ranges[@]}" >&2
  exit 1
fi
ip_range="${ranges[0]}"

mapfile -t pinned < <(sed -n 's/^[[:space:]]*ipv4_address:[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' <<< "$config")
if [ "${#pinned[@]}" -eq 0 ]; then
  echo "FAIL: no service pins an ipv4_address, so this check has nothing to protect" >&2
  exit 1
fi

# Every pinned address must sit outside the pool Docker assigns from.
for address in "${pinned[@]}"; do
  verdict="$(python3 -c '
import ipaddress, sys
print("inside" if ipaddress.ip_address(sys.argv[1]) in ipaddress.ip_network(sys.argv[2]) else "outside")
' "$address" "$ip_range")"
  if [ "$verdict" = "inside" ]; then
    echo "FAIL: pinned address $address lies inside the dynamic range $ip_range" >&2
    exit 1
  fi
done

echo "Network pin check passed (${#pinned[@]} pinned addresses outside $ip_range)"
