#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
# Shell functions for NetBox Interface Name Rules Plugin development.
# Usage: source .devcontainer/scripts/load-aliases.sh

export PATH="/opt/netbox/venv/bin:$PATH"
export DEBUG="${DEBUG:-True}"
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Unset CA bundle vars when empty (Compose/devcontainer inject "" when the
# host var is unset, which breaks requests/curl).
for _ca_var in REQUESTS_CA_BUNDLE SSL_CERT_FILE CURL_CA_BUNDLE; do
  if [ -z "${!_ca_var}" ]; then
    unset "$_ca_var"
  fi
done
unset _ca_var

# Load shared process management helpers
if ! source "$PLUGIN_DIR/.devcontainer/scripts/process-helpers.sh"; then
  printf '%s\n' "Failed to load process-helpers.sh" >&2
  return 1
fi

netbox-run-bg() { "$PLUGIN_DIR/.devcontainer/scripts/start-netbox.sh" --background; }
netbox-run()    { "$PLUGIN_DIR/.devcontainer/scripts/start-netbox.sh"; }

# Robust stop command that kills both tracked and orphaned processes
netbox-stop() {
  echo "🛑 Stopping NetBox and RQ workers..."
  if [ -f /tmp/netbox.pid ]; then
    local PID
    PID=$(cat /tmp/netbox.pid 2>/dev/null)
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
      graceful_kill_pid "$PID"
      echo "   Stopped NetBox (PID: $PID)"
    fi
    rm -f /tmp/netbox.pid
  fi
  if [ -f /tmp/rqworker.pid ]; then
    local PID
    PID=$(cat /tmp/rqworker.pid 2>/dev/null)
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
      graceful_kill_pid "$PID"
      echo "   Stopped RQ worker (PID: $PID)"
    fi
    rm -f /tmp/rqworker.pid
  fi
  if pgrep -f "python.*rqworker" >/dev/null 2>&1; then
    local ORPHAN_COUNT
    ORPHAN_COUNT=$(pgrep -cf "python.*rqworker" 2>/dev/null || echo 0)
    graceful_kill_pattern "python.*rqworker"
    echo "   Killed $ORPHAN_COUNT orphaned RQ worker(s)"
  fi
  if pgrep -f "python.*runserver.*8000" >/dev/null 2>&1; then
    graceful_kill_pattern "python.*runserver.*8000"
    echo "   Killed orphaned NetBox server(s)"
  fi
  echo "✅ All processes stopped"
}

netbox-restart() {
  netbox-stop && sleep 1 && netbox-run-bg
}

netbox-reload() {
  cd "$PLUGIN_DIR" || return 1
  if command -v uv >/dev/null 2>&1; then
    uv pip install -e . || return 1
  else
    pip install -e . || return 1
  fi
  netbox-restart
}

alias netbox-logs="tail -f /tmp/netbox.log"
alias rq-logs="tail -f /tmp/rqworker.log"

netbox-status() {
  local PID
  if [ -f /tmp/netbox.pid ]; then
    PID=$(cat /tmp/netbox.pid 2>/dev/null)
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
      echo "NetBox is running (PID: $PID)"
    else
      echo "NetBox is not running"
    fi
  else
    echo "NetBox is not running"
  fi
  if [ -f /tmp/rqworker.pid ]; then
    PID=$(cat /tmp/rqworker.pid 2>/dev/null)
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
      echo "RQ worker is running (PID: $PID)"
    else
      echo "RQ worker is not running"
    fi
  else
    echo "RQ worker is not running"
  fi
}

rq-status() {
  local PID
  if [ -f /tmp/rqworker.pid ]; then
    PID=$(cat /tmp/rqworker.pid 2>/dev/null)
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
      echo "RQ worker is running (PID: $PID)"
    else
      echo "RQ worker is not running"
    fi
  else
    echo "RQ worker is not running"
  fi
}

netbox-shell() {
  cd /opt/netbox/netbox && source /opt/netbox/venv/bin/activate && python manage.py shell
}

netbox-test() {
  cd /opt/netbox/netbox && source /opt/netbox/venv/bin/activate && python manage.py test netbox_interface_name_rules "$@"
}

# Run tests on a per-session ISOLATED test database, so concurrent suites in the
# shared devcontainer don't collide on test_netbox (which corrupts migrations and
# can leave lock-holding zombie connections). Pass the app(s) + any test flags,
# e.g.  netbox-test-isolated netbox_nso_plugin --keepdb
# Override the DB name with TEST_DB_NAME=...; otherwise it's derived from the first
# app argument (a stable name so --keepdb can reuse it).
netbox-test-isolated() {
  local first_app="netbox_interface_name_rules"
  case "${1:-}" in -*|"") ;; *) first_app="$1" ;; esac
  local db_name="${TEST_DB_NAME:-test_$(printf '%s' "$first_app" | tr -c 'a-zA-Z0-9' '_')}"
  cd /opt/netbox/netbox && source /opt/netbox/venv/bin/activate && \
    PYTHONPATH="$PLUGIN_DIR/.devcontainer/config${PYTHONPATH:+:$PYTHONPATH}" \
    TEST_DB_NAME="$db_name" \
    python manage.py test "$@" --settings=isolated_test_settings
}

netbox-manage() {
  cd /opt/netbox/netbox && source /opt/netbox/venv/bin/activate && python manage.py "$@"
}

plugin-install() {
  cd "$PLUGIN_DIR" || return 1
  if command -v uv >/dev/null 2>&1; then
    uv pip install -e .
  else
    pip install -e .
  fi
}

plugins-install() {
  if [ -f "$PLUGIN_DIR/.devcontainer/extra-requirements.txt" ]; then
    source /opt/netbox/venv/bin/activate
    if command -v uv >/dev/null 2>&1; then
      uv pip install -r "$PLUGIN_DIR/.devcontainer/extra-requirements.txt"
    else
      pip install -r "$PLUGIN_DIR/.devcontainer/extra-requirements.txt"
    fi
  else
    echo "No .devcontainer/extra-requirements.txt found"
  fi
}

ruff-check() { cd "$PLUGIN_DIR" && command ruff check .; }
ruff-format() { cd "$PLUGIN_DIR" && command ruff format .; }
ruff-fix()    { cd "$PLUGIN_DIR" && command ruff check --fix .; }

diagnose() { "$PLUGIN_DIR/.devcontainer/scripts/diagnose.sh"; }

# RQ job inspection commands
rq-stats() {
  cd /opt/netbox/netbox && source /opt/netbox/venv/bin/activate && python manage.py rqstats
}

rq-jobs() {
  cd /opt/netbox/netbox && source /opt/netbox/venv/bin/activate && python manage.py shell -c \
    "from django_rq import get_queue; q = get_queue('default'); print(f'Jobs in queue: {len(q)}'); [print(f'  {job.id[:8]}: {job.func_name} - {job.get_status()}') for job in q.jobs[:10]]"
}

rq-failed() {
  cd /opt/netbox/netbox && source /opt/netbox/venv/bin/activate && python manage.py shell -c \
    "from django_rq import get_failed_queue; q = get_failed_queue(); print(f'Failed jobs: {len(q)}'); [print(f'  {job.id[:8]}: {job.func_name}') for job in q.jobs[:10]]"
}

rq-recent() {
  cd /opt/netbox/netbox && source /opt/netbox/venv/bin/activate && python manage.py shell -c \
    "from core.models import Job; jobs = Job.objects.all().order_by('-created')[:10]; [print(f'{j.id}: {j.name[:50]} - {getattr(j.status, \"value\", j.status)} ({j.user})') for j in jobs]"
}

# Help
dev-help() {
  echo "🎯 NetBox Interface Name Rules Plugin Development Commands:"
  echo ""
  echo "📊 NetBox Server Management:"
  echo "  netbox-run-bg       : Start NetBox in background"
  echo "  netbox-run          : Start NetBox in foreground (for debugging)"
  echo "  netbox-stop         : Stop NetBox and RQ worker"
  echo "  netbox-restart      : Restart NetBox and RQ worker"
  echo "  netbox-reload       : Reinstall plugin and restart NetBox"
  echo "  netbox-status       : Check if NetBox and RQ worker are running"
  echo "  netbox-logs         : View NetBox server logs"
  echo ""
  echo "⚙️  Background Jobs (RQ Worker):"
  echo "  rq-status           : Check if RQ worker is running"
  echo "  rq-logs             : View RQ worker logs"
  echo "  rq-stats            : Show RQ queue statistics"
  echo "  rq-jobs             : List jobs in default queue"
  echo "  rq-failed           : List failed jobs"
  echo "  rq-recent           : Show recent NetBox jobs"
  echo ""
  echo "🛠️  Development Tools:"
  echo "  netbox-shell        : Open NetBox Django shell"
  echo "  netbox-test         : Run plugin tests"
  echo "  netbox-test-isolated: Run tests on a per-session isolated test DB (concurrency-safe)"
  echo "  netbox-manage       : Run Django management commands"
  echo "  plugin-install      : Reinstall plugin in development mode"
  echo ""
  echo "🧹 Code Quality:"
  echo "  ruff-check          : Check code with Ruff"
  echo "  ruff-format         : Format code with Ruff"
  echo "  ruff-fix            : Auto-fix code issues with Ruff"
  echo ""
  echo "🔎 Diagnostics:"
  echo "  diagnose            : Run startup diagnostics"
  echo "  dev-help            : Show this help message"
  echo ""
  echo "📖 NetBox available at: http://localhost:8000 (${SUPERUSER_NAME:-admin}/${SUPERUSER_PASSWORD:-admin})"
}

echo "✅ Dev helpers loaded! Try: rq-status, rq-stats, rq-recent, dev-help"
