#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

echo "🔍 DevContainer Startup Diagnostics"
echo "=================================="

echo "📍 Current working directory: $(pwd)"
echo "👤 Current user: $(whoami)"

echo ""
echo "🐳 Container Environment:"
echo "  - NETBOX_VERSION: ${NETBOX_VERSION:-not set}"
echo "  - DEBUG: ${DEBUG:-not set}"
echo "  - DB_HOST: ${DB_HOST:-not set}"
echo "  - REDIS_HOST: ${REDIS_HOST:-not set}"

echo ""
echo "🔗 Service Connectivity:"
echo "  - PostgreSQL: $(timeout 3 bash -c 'cat < /dev/null > /dev/tcp/postgres/5432' 2>/dev/null && echo 'Connected' || echo 'Not reachable')"
echo "  - Redis: $(timeout 3 bash -c 'cat < /dev/null > /dev/tcp/redis/6379' 2>/dev/null && echo 'Connected' || echo 'Not reachable')"

echo ""
echo "🗂️ File System:"
echo "  - NetBox venv: $(test -f /opt/netbox/venv/bin/activate && echo 'Exists' || echo 'Missing')"
echo "  - Plugin directory: $(test -d /workspaces/netbox-InterfaceNameRules-plugin && echo 'Exists' || echo 'Missing')"
echo "  - Plugin config: $(test -f /workspaces/netbox-InterfaceNameRules-plugin/.devcontainer/config/plugin-config.py && echo 'Found' || echo 'Missing (using defaults)')"
echo "  - Extra plugins config: $(test -f /workspaces/netbox-InterfaceNameRules-plugin/.devcontainer/config/extra-plugins.py && echo 'Found' || echo 'Not configured')"

echo ""
echo "🚀 Process Status:"
if [ -f /tmp/netbox.pid ]; then
  PID=$(cat /tmp/netbox.pid)
  if kill -0 "$PID" 2>/dev/null; then
    echo "  - NetBox server: Running (PID: $PID)"
  else
    echo "  - NetBox server: PID file exists but process not running"
  fi
else
  echo "  - NetBox server: Not started"
fi

echo ""
echo "🌍 Port Check:"
if command -v ss >/dev/null 2>&1; then
  echo "  - Port 8000: $(ss -tuln 2>/dev/null | grep :8000 >/dev/null && echo 'Listening' || echo 'Not listening')"
elif command -v netstat >/dev/null 2>&1; then
  echo "  - Port 8000: $(netstat -tuln 2>/dev/null | grep :8000 >/dev/null && echo 'Listening' || echo 'Not listening')"
fi

echo ""
echo "✅ Diagnostic complete!"
