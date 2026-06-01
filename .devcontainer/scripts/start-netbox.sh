#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

# Check if we should run in background or foreground
BACKGROUND=false
if [ "$1" = "--background" ] || [ "$1" = "-b" ]; then
  BACKGROUND=true
fi

echo "🌐 Starting NetBox development server..."

# Set required environment variables
export DEBUG="${DEBUG:-True}"

# Detect Codespaces and set access URL
if [ "$CODESPACES" = "true" ] && [ -n "$CODESPACE_NAME" ]; then
  GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN="${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
  ACCESS_URL="https://${CODESPACE_NAME}-8000.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
  echo "🔗 GitHub Codespaces detected"
else
  ACCESS_URL="http://localhost:8000"
fi

# Load shared process management helpers
if ! source "$(dirname "$0")/process-helpers.sh"; then
  echo "ERROR: Failed to load process-helpers.sh" >&2
  exit 1
fi

# Kill any orphaned processes (not tracked by PID file)
echo "🧹 Cleaning up orphaned processes..."
# Kill the watchmedo autoreload supervisor first: if a worker is killed while
# watchmedo is still alive, watchmedo just respawns it. Killing the parent first
# avoids that race (it also stops its child worker on shutdown).
if pgrep -f "watchmedo.*rqworker" >/dev/null 2>&1; then
  echo "   Found orphaned watchmedo RQ supervisors, killing..."
  graceful_kill_pattern "watchmedo.*rqworker"
fi
if pgrep -f "python.*rqworker" >/dev/null 2>&1; then
  echo "   Found orphaned RQ workers, killing..."
  graceful_kill_pattern "python.*rqworker"
fi

if pgrep -f "python.*runserver.*8000" >/dev/null 2>&1; then
  echo "   Found orphaned NetBox servers, killing..."
  graceful_kill_pattern "python.*runserver.*8000"
fi

# Stop any tracked processes from PID files
if [ -f /tmp/netbox.pid ]; then
  OLD_PID=$(cat /tmp/netbox.pid 2>/dev/null)
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    graceful_kill_pid "$OLD_PID"
  fi
  rm -f /tmp/netbox.pid
fi

if [ -f /tmp/rqworker.pid ]; then
  OLD_PID=$(cat /tmp/rqworker.pid 2>/dev/null)
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    graceful_kill_pid "$OLD_PID"
  fi
  rm -f /tmp/rqworker.pid
fi

# Activate NetBox virtual environment
source /opt/netbox/venv/bin/activate

# Navigate to NetBox directory
if ! cd /opt/netbox/netbox; then
  echo "ERROR: Failed to cd into /opt/netbox/netbox" >&2
  exit 1
fi

# Start RQ worker in background
# Dev environment: keep startup simple — no service manager needed.
echo "⚙️  Starting RQ worker..."
(
  source /opt/netbox/venv/bin/activate
  if ! cd /opt/netbox/netbox; then
    echo "ERROR: RQ worker subshell: failed to cd into /opt/netbox/netbox" >&2
    exit 1
  fi
  # rqworker has no built-in autoreload (unlike `runserver`), so plugin code
  # edits don't take effect until the worker is restarted — stale workers
  # silently serve old code for background jobs. Wrap it with watchmedo
  # (watchdog) to restart on *.py changes under the workspace; fall back to a
  # plain worker if watchmedo isn't installed. Override the watched path with
  # RQWORKER_WATCH_DIR.
  RQWORKER_WATCH_DIR="${RQWORKER_WATCH_DIR:-/workspaces}"
  if command -v watchmedo >/dev/null 2>&1; then
    echo "   (autoreload via watchmedo, watching ${RQWORKER_WATCH_DIR})"
    exec watchmedo auto-restart \
      --directory="${RQWORKER_WATCH_DIR}" --pattern='*.py' --recursive \
      -- python manage.py rqworker --verbosity=1
  else
    exec python manage.py rqworker --verbosity=1
  fi
) > /tmp/rqworker.log 2>&1 &

RQ_PID=$!
echo $RQ_PID > /tmp/rqworker.pid
echo "✅ RQ worker started (PID: $RQ_PID)"

if [ "$BACKGROUND" = true ]; then
  echo "🚀 Starting NetBox in background"
  (
    export DEBUG="${DEBUG:-True}"
    source /opt/netbox/venv/bin/activate
    if ! cd /opt/netbox/netbox; then
      echo "ERROR: NetBox subshell: failed to cd into /opt/netbox/netbox" >&2
      exit 1
    fi
    python manage.py runserver 0.0.0.0:8000 --verbosity=0
  ) > /tmp/netbox.log 2>&1 &

  NETBOX_PID=$!
  echo $NETBOX_PID > /tmp/netbox.pid
  echo "✅ NetBox started in background (PID: $NETBOX_PID)"
  echo "📍 Access NetBox at: $ACCESS_URL"
  echo "💡 If clicking the URL opens 0.0.0.0:8000, manually type: localhost:8000"
  echo "📄 View logs with: netbox-logs"
  echo "🛑 Stop NetBox with: netbox-stop"
else
  echo "🌍 Starting NetBox in foreground"
  echo "📍 Access NetBox at: $ACCESS_URL"
  echo "💡 If clicking the URL opens 0.0.0.0:8000, manually type: localhost:8000"
  echo ""
  python manage.py runserver 0.0.0.0:8000
fi
