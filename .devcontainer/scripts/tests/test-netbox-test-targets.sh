#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba

set -eo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT
export PYTEST_ARGV="$TEST_DIR/argv"

source() {
  if [ "$1" = /opt/netbox/venv/bin/activate ]; then
    return 0
  fi
  builtin source "$@"
}
source "$REPO_ROOT/.devcontainer/scripts/load-aliases.sh"
cat > "$TEST_DIR/pytest" <<'STUB'
#!/bin/bash
printf '%s\n' "$@" > "$PYTEST_ARGV"
STUB
chmod +x "$TEST_DIR/pytest"
export PATH="$TEST_DIR:$PATH"

netbox-test
printf '%s\n' netbox_interface_name_rules > "$TEST_DIR/expected"
diff -u "$TEST_DIR/expected" "$PYTEST_ARGV"

netbox-test netbox_interface_name_rules/tests/test_rules.py -k 'a test name'
printf '%s\n' netbox_interface_name_rules/tests/test_rules.py -k 'a test name' > "$TEST_DIR/expected"
diff -u "$TEST_DIR/expected" "$PYTEST_ARGV"
printf '%s\n' 'NetBox test target check passed'
