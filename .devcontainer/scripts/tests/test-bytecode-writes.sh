#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
# shellcheck source=.devcontainer/scripts/tests/lib.sh
source "$(dirname "$0")/lib.sh"
compose_file_args "$REPO_ROOT"

# The container runs as root and mounts the host checkout, so any bytecode Python caches lands in
# the developer's tree owned by root. Those files can block `git worktree remove` and host tooling.
docker compose "${COMPOSE_FILES[@]}" config --format json | python3 -c "
import json
import sys

configuration = json.load(sys.stdin)
services = configuration.get('services', {})

failures = []
for name, service in services.items():
    # Only services that mount the host checkout can write into it.
    mounts = service.get('volumes') or []
    if not any(str(mount.get('target', '')).startswith('/workspaces') for mount in mounts):
        continue
    value = (service.get('environment') or {}).get('PYTHONDONTWRITEBYTECODE')
    if str(value) != '1':
        failures.append(f'{name} mounts the checkout but sets PYTHONDONTWRITEBYTECODE={value!r}')

if failures:
    for failure in failures:
        print(f'FAIL: {failure}', file=sys.stderr)
    print(
        'Root-owned .pyc files would accumulate in the host tree. '
        'Set PYTHONDONTWRITEBYTECODE: \"1\" on that service.',
        file=sys.stderr,
    )
    raise SystemExit(1)

if not services:
    print('FAIL: the compose file declares no services', file=sys.stderr)
    raise SystemExit(1)

print('Bytecode-write check passed (no checkout-mounting service caches bytecode)')
"
