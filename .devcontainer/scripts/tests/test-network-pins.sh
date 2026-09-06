#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
# shellcheck source=.devcontainer/scripts/tests/lib.sh
source "$(dirname "$0")/lib.sh"
compose_file_args "$REPO_ROOT"

config="$(docker compose "${COMPOSE_FILES[@]}" config)"

ip_range="$(sed -n 's/^[[:space:]]*ip_range:[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' <<< "$config")"
if [ -z "$ip_range" ]; then
  echo "FAIL: the default network declares no ip_range, so Docker may hand a pinned address to another service" >&2
  exit 1
fi

mapfile -t pinned < <(sed -n 's/^[[:space:]]*ipv4_address:[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' <<< "$config")
if [ "${#pinned[@]}" -eq 0 ]; then
  echo "FAIL: no service pins an ipv4_address, so this check has nothing to protect" >&2
  exit 1
fi

# Every pinned address must sit outside the pool Docker assigns from.
for address in "${pinned[@]}"; do
  if python3 -c "
import ipaddress, sys
sys.exit(0 if ipaddress.ip_address('$address') in ipaddress.ip_network('$ip_range') else 1)
"; then
    echo "FAIL: pinned address $address lies inside the dynamic range $ip_range" >&2
    exit 1
  fi
done

echo "Network pin check passed (${#pinned[@]} pinned addresses outside $ip_range)"
