#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

set -eo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
TEST_DIR="$(mktemp -d)"
RUNNER_PIDS=()
cleanup() {
  local marker pid
  for marker in "$TEST_DIR"/*-started; do
    if [ -f "$marker" ]; then
      read -r pid < "$marker"
      kill "$pid" 2>/dev/null || true
    fi
  done
  for pid in "${RUNNER_PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
  rm -rf "$TEST_DIR"
}
trap cleanup EXIT
export PYTEST_ARGV="$TEST_DIR/argv"

wait_for_file() {
  local path="$1"
  local attempt
  for attempt in {1..100}; do
    if [ -f "$path" ]; then
      return 0
    fi
    sleep 0.05
  done
  echo "Timed out waiting for $path" >&2
  return 1
}

wait_for_flock() {
  local pid="$1"
  local attempt command
  for attempt in {1..100}; do
    command="$(cat "/proc/$pid/comm" 2>/dev/null || true)"
    if [ "$command" = flock ] || pgrep -P "$pid" -x flock >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.05
  done
  echo "Timed out waiting for netbox-test $pid to acquire the test-run lease" >&2
  return 1
}

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
if [ -n "${PYTEST_STARTED:-}" ]; then
  printf '%s\n' "$$" > "$PYTEST_STARTED"
fi
if [ -n "${PYTEST_RELEASE:-}" ]; then
  read -r _ < "$PYTEST_RELEASE"
fi
exit "${PYTEST_EXIT_STATUS:-0}"
STUB
chmod +x "$TEST_DIR/pytest"
export PATH="$TEST_DIR:$PATH"

netbox-test
printf '%s\n' netbox_interface_name_rules > "$TEST_DIR/expected"
diff -u "$TEST_DIR/expected" "$PYTEST_ARGV"

netbox-test netbox_interface_name_rules/tests/test_rules.py -k 'a test name'
printf '%s\n' netbox_interface_name_rules/tests/test_rules.py -k 'a test name' > "$TEST_DIR/expected"
diff -u "$TEST_DIR/expected" "$PYTEST_ARGV"

mkfifo "$TEST_DIR/first-release"
PYTEST_ARGV="$TEST_DIR/first-argv" \
PYTEST_STARTED="$TEST_DIR/first-started" \
PYTEST_RELEASE="$TEST_DIR/first-release" \
PYTEST_EXIT_STATUS=23 \
TEST_DB_NAME=test_first_run \
TEST_REDIS_HOST=redis \
  netbox-test "first argument" "" &
first_runner=$!
RUNNER_PIDS+=("$first_runner")
wait_for_file "$TEST_DIR/first-started"

PYTEST_ARGV="$TEST_DIR/second-argv" \
PYTEST_STARTED="$TEST_DIR/second-started" \
TEST_DB_NAME=test_second_run \
TEST_REDIS_HOST=redis \
  netbox-test "second argument" "*" &
second_runner=$!
RUNNER_PIDS+=("$second_runner")
wait_for_flock "$second_runner"
if [ -e "$TEST_DIR/second-started" ]; then
  echo "A second netbox-test entered pytest while the first invocation was running" >&2
  exit 1
fi

printf '%s\n' release > "$TEST_DIR/first-release"
if wait "$first_runner"; then
  first_status=0
else
  first_status=$?
fi
if [ "$first_status" -ne 23 ]; then
  echo "netbox-test returned $first_status instead of pytest status 23" >&2
  exit 1
fi
wait_for_file "$TEST_DIR/second-started"
wait "$second_runner"

printf '%s\n' "first argument" "" > "$TEST_DIR/first-expected"
diff -u "$TEST_DIR/first-expected" "$TEST_DIR/first-argv"
printf '%s\n' "second argument" "*" > "$TEST_DIR/second-expected"
diff -u "$TEST_DIR/second-expected" "$TEST_DIR/second-argv"
printf '%s\n' 'NetBox test target check passed'
