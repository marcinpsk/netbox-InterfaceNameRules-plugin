#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

# Check if we should run in background or foreground
BACKGROUND=false
if [ "$1" = "--background" ] || [ "$1" = "-b" ]; then
  BACKGROUND=true
fi

echo "🌐 Starting NetBox development server..."

export DEBUG="${DEBUG:-True}"

if [ "$CODESPACES" = "true" ] && [ -n "$CODESPACE_NAME" ]; then
  GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN="${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
  ACCESS_URL="https://${CODESPACE_NAME}-8000.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
else
  ACCESS_URL="http://localhost:8000"
fi

# Clean up orphaned processes
echo "🧹 Cleaning up orphaned processes..."
if pgrep -f "python.*runserver.*8000" >/dev/null 2>&1; then
  pkill -9 -f "python.*runserver.*8000" 2>/dev/null
  sleep 1
fi

if [ -f /tmp/netbox.pid ]; then
  OLD_PID=$(cat /tmp/netbox.pid 2>/dev/null)
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    kill "$OLD_PID" 2>/dev/null || kill -9 "$OLD_PID" 2>/dev/null
  fi
  rm -f /tmp/netbox.pid
fi

source /opt/netbox/venv/bin/activate
cd /opt/netbox/netbox

if [ "$BACKGROUND" = true ]; then
  echo "🚀 Starting NetBox in background"
  (
    export DEBUG="${DEBUG:-True}"
    source /opt/netbox/venv/bin/activate
    cd /opt/netbox/netbox
    python manage.py runserver 0.0.0.0:8000 --verbosity=0
  ) > /tmp/netbox.log 2>&1 &

  NETBOX_PID=$!
  echo $NETBOX_PID > /tmp/netbox.pid
  echo "✅ NetBox started in background (PID: $NETBOX_PID)"
  echo "📍 Access NetBox at: $ACCESS_URL"
  echo "📄 View logs with: netbox-logs"
  echo "🛑 Stop with: netbox-stop"
else
  echo "🌍 Starting NetBox in foreground"
  echo "📍 Access NetBox at: $ACCESS_URL"
  echo ""
  python manage.py runserver 0.0.0.0:8000
fi
