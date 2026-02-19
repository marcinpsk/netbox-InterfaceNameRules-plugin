#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
# Shell functions for NetBox Interface Name Rules Plugin development.
# Usage: source .devcontainer/scripts/load-aliases.sh

export PATH="/opt/netbox/venv/bin:$PATH"
export DEBUG="${DEBUG:-True}"
PLUGIN_DIR="/workspaces/netbox-InterfaceNameRules-plugin"

netbox-run-bg() {
  "$PLUGIN_DIR/.devcontainer/scripts/start-netbox.sh" --background
}

netbox-run() {
  "$PLUGIN_DIR/.devcontainer/scripts/start-netbox.sh"
}

netbox-stop() {
  echo "🛑 Stopping NetBox..."
  if [ -f /tmp/netbox.pid ]; then
    local pid
    pid=$(cat /tmp/netbox.pid 2>/dev/null)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null
      echo "   Stopped NetBox (PID: $pid)"
    fi
    rm -f /tmp/netbox.pid
  fi
  if pgrep -f "python.*runserver.*8000" >/dev/null 2>&1; then
    pkill -9 -f "python.*runserver.*8000" 2>/dev/null
    echo "   Killed orphaned NetBox server(s)"
  fi
  echo "✅ All processes stopped"
}

netbox-restart() {
  netbox-stop
  sleep 1
  netbox-run-bg
}

netbox-reload() {
  cd "$PLUGIN_DIR" && uv pip install -e . && netbox-restart
}

netbox-logs() {
  tail -f /tmp/netbox.log
}

netbox-status() {
  if [ -f /tmp/netbox.pid ] && kill -0 "$(cat /tmp/netbox.pid)" 2>/dev/null; then
    echo "NetBox is running (PID: $(cat /tmp/netbox.pid))"
  else
    echo "NetBox is not running"
  fi
}

netbox-shell() {
  (cd /opt/netbox/netbox && source /opt/netbox/venv/bin/activate && python manage.py shell)
}

netbox-test() {
  (cd /opt/netbox/netbox && source /opt/netbox/venv/bin/activate && python manage.py test netbox_interface_name_rules "$@")
}

netbox-manage() {
  (cd /opt/netbox/netbox && source /opt/netbox/venv/bin/activate && python manage.py "$@")
}

plugin-install() {
  (cd "$PLUGIN_DIR" && uv pip install -e .)
}

ruff-check() {
  (cd "$PLUGIN_DIR" && ruff check .)
}

ruff-format() {
  (cd "$PLUGIN_DIR" && ruff format .)
}

ruff-fix() {
  (cd "$PLUGIN_DIR" && ruff check --fix .)
}

diagnose() {
  "$PLUGIN_DIR/.devcontainer/scripts/diagnose.sh"
}

dev-help() {
  echo "🎯 NetBox Interface Name Rules Plugin Development Commands:"
  echo ""
  echo "📊 NetBox Server Management:"
  echo "  netbox-run-bg       Start NetBox in background"
  echo "  netbox-run          Start NetBox in foreground"
  echo "  netbox-stop         Stop NetBox"
  echo "  netbox-restart      Restart NetBox"
  echo "  netbox-reload       Reinstall plugin and restart"
  echo "  netbox-status       Check if NetBox is running"
  echo "  netbox-logs         View NetBox server logs"
  echo ""
  echo "🛠️  Development Tools:"
  echo "  netbox-shell        Open NetBox Django shell"
  echo "  netbox-test         Run plugin tests"
  echo "  netbox-manage CMD   Run Django management commands"
  echo "  plugin-install      Reinstall plugin in dev mode"
  echo ""
  echo "🧹 Code Quality:"
  echo "  ruff-check          Check code with Ruff"
  echo "  ruff-format         Format code with Ruff"
  echo "  ruff-fix            Auto-fix code issues"
  echo ""
  echo "🔎 Diagnostics:"
  echo "  diagnose            Run startup diagnostics"
  echo "  dev-help            Show this help message"
  echo ""
  echo "📖 NetBox at: http://localhost:8000 (admin/admin)"
}

echo "✅ Functions loaded! Type 'dev-help' for available commands."
