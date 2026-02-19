#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

# Ensure aliases are available
source "$(dirname "$0")/load-aliases.sh" 2>/dev/null

echo ""
echo "🎯 NetBox Interface Name Rules Plugin Development Environment"

if [ ! -f "/workspaces/netbox-InterfaceNameRules-plugin/.devcontainer/config/plugin-config.py" ]; then
  echo ""
  echo "⚠️  Plugin configuration not found: .devcontainer/config/plugin-config.py"
  echo "   Create it: cp .devcontainer/config/plugin-config.py.example .devcontainer/config/plugin-config.py"
fi

echo ""
if command -v gh >/dev/null 2>&1; then
  if gh auth status >/dev/null 2>&1; then
    GH_USER=$(gh api user --jq '.login' 2>/dev/null || echo "unknown")
    echo "✅ GitHub authenticated as: $GH_USER"
  else
    echo "🔑 GitHub CLI available but not authenticated"
    echo "   Run 'gh auth login' to authenticate"
  fi
fi

echo ""
if [ -n "$CODESPACES" ]; then
  echo "🌐 GitHub Codespaces Environment"
else
  echo "🖥️  Local Development Environment"
  echo "   NetBox will be available at: http://localhost:8000"
fi

echo ""
echo "🚀 Quick start:"
echo "   • Type 'netbox-run' to start the development server"
echo "   • Type 'dev-help' to see all available commands"
echo ""
