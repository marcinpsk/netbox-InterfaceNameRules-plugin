#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
set -e

PLUGIN_NAME="netbox_interface_name_rules"
PLUGIN_DISPLAY="Interface Name Rules"

echo "🚀 Setting up NetBox ${PLUGIN_DISPLAY} Plugin development environment..."
echo "📍 Current working directory: $(pwd)"
NETBOX_VERSION=${NETBOX_VERSION:-"latest"}
echo "📦 Using NetBox Docker image: netboxcommunity/netbox:${NETBOX_VERSION}"

# Detect plugin workspace directory
detect_plugin_workspace() {
  if [ -f "$PWD/pyproject.toml" ]; then
    echo "$PWD"
  elif [ -d "/workspaces/netbox-InterfaceNameRules-plugin" ] && [ -f "/workspaces/netbox-InterfaceNameRules-plugin/pyproject.toml" ]; then
    echo "/workspaces/netbox-InterfaceNameRules-plugin"
  else
    local candidate
    candidate=$(find /workspaces -maxdepth 2 -type f -name pyproject.toml 2>/dev/null | head -n1 | xargs -r dirname || true)
    if [ -n "$candidate" ] && [ -f "$candidate/pyproject.toml" ]; then
      echo "$candidate"
    else
      echo ""
    fi
  fi
}

# Proxy/CA setup
if [ -n "$HTTP_PROXY" ] || [ -n "$HTTPS_PROXY" ]; then
  echo "🌐 Configuring proxy settings..."
  [ -n "$HTTP_PROXY" ] && echo "Acquire::http::Proxy \"$HTTP_PROXY\";" > /etc/apt/apt.conf.d/80proxy
  [ -n "$HTTPS_PROXY" ] && echo "Acquire::https::Proxy \"$HTTPS_PROXY\";" >> /etc/apt/apt.conf.d/80proxy
  export HTTP_PROXY HTTPS_PROXY http_proxy https_proxy NO_PROXY no_proxy

  PLUGIN_WS_DIR_EARLY="$(detect_plugin_workspace)"
  [ -z "$PLUGIN_WS_DIR_EARLY" ] && PLUGIN_WS_DIR_EARLY="/workspaces/netbox-InterfaceNameRules-plugin"

  # Try CA bundles from both plugin workspaces
  CA_BUNDLE_SRC=""
  for ca_path in "$PLUGIN_WS_DIR_EARLY/ca-bundle.crt" "/workspaces/netbox-librenms-plugin/ca-bundle.crt"; do
    if [ -f "$ca_path" ]; then
      CA_BUNDLE_SRC="$ca_path"
      break
    fi
  done

  if [ -n "$CA_BUNDLE_SRC" ]; then
    echo "🔐 Installing custom CA certificate from $CA_BUNDLE_SRC..."
    mkdir -p /usr/local/share/ca-certificates/proxy
    find /usr/local/share/ca-certificates/proxy -maxdepth 1 -name 'cert-*' -delete 2>/dev/null || true
    csplit -z -f /usr/local/share/ca-certificates/proxy/cert- "$CA_BUNDLE_SRC" '/-----BEGIN CERTIFICATE-----/' '{*}' >/dev/null 2>&1
    for f in /usr/local/share/ca-certificates/proxy/cert-*; do mv "$f" "${f}.crt" 2>/dev/null || true; done
    update-ca-certificates 2>/dev/null
    export REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
    export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
    export CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
    export GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt
    pip config set global.cert /etc/ssl/certs/ca-certificates.crt 2>/dev/null || true
    echo "  ✓ CA certificate installed"
  fi
fi

# Activate venv
if [ ! -f "/opt/netbox/venv/bin/activate" ]; then
    echo "❌ NetBox virtual environment not found"
    exit 1
fi
source /opt/netbox/venv/bin/activate

# Install uv if not available
if ! command -v uv >/dev/null 2>&1; then
  echo "🔧 Installing uv..."
  pip install uv
fi

echo "🔧 Installing development dependencies..."
apt-get update -qq
apt-get install -y -qq net-tools git
uv pip install pytest pytest-django ruff pre-commit

# Install GitHub CLI
if ! command -v gh >/dev/null 2>&1; then
  echo "🔧 Installing GitHub CLI..."
  (type -p wget >/dev/null || apt-get install -y -qq wget) \
    && install -d -m 755 /etc/apt/keyrings \
    && out=$(mktemp) \
    && wget -qO "$out" https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    && cat "$out" | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update -qq \
    && apt-get install -y -qq gh \
    && rm -f "$out" \
    && echo "  ✓ GitHub CLI installed" \
    || echo "⚠️  GitHub CLI installation failed (non-fatal)"
fi

# Detect and install this plugin
PLUGIN_WS_DIR="$(detect_plugin_workspace)"
if [ -z "$PLUGIN_WS_DIR" ]; then
  echo "❌ Could not locate plugin workspace directory"
  exit 1
fi
echo "📂 Plugin workspace: $PLUGIN_WS_DIR"
cd "$PLUGIN_WS_DIR"
uv pip install -e .
echo "✅ Installed $PLUGIN_NAME in editable mode"

# Install extra plugins from config file
EXTRA_PLUGINS_CFG="$PLUGIN_WS_DIR/.devcontainer/config/extra-plugins.py"
if [ -f "$EXTRA_PLUGINS_CFG" ]; then
  echo "📦 Processing extra plugins..."
  python3 -c "
import sys
sys.path.insert(0, '.')
spec = __import__('importlib.util', fromlist=['util'])
s = spec.spec_from_file_location('extra_plugins', '$EXTRA_PLUGINS_CFG')
m = spec.module_from_spec(s)
s.loader.exec_module(m)
for p in getattr(m, 'EXTRA_PLUGINS', []):
    name = p.get('name', '')
    source = p.get('source', 'pip')
    if not name:
        continue
    if source == 'pip':
        print(f'pip:{name}')
    else:
        print(f'editable:{source}')
" 2>/dev/null | while IFS= read -r line; do
    if [[ "$line" == pip:* ]]; then
      pkg="${line#pip:}"
      echo "  📦 Installing $pkg from PyPI..."
      uv pip install "$pkg" || echo "  ⚠️  Failed to install $pkg"
    elif [[ "$line" == editable:* ]]; then
      path="${line#editable:}"
      echo "  📦 Installing from $path (editable)..."
      if [ -d "$path" ]; then
        uv pip install -e "$path" || echo "  ⚠️  Failed to install from $path"
      else
        echo "  ⚠️  Path $path not found (is the repo mounted?)"
      fi
    fi
  done
fi

# Inject plugin configuration into NetBox
CONF_FILE="/opt/netbox/netbox/netbox/configuration.py"
if [ -f "$CONF_FILE" ]; then
  if ! grep -q "# Devcontainer Plugins Loader" "$CONF_FILE" 2>/dev/null; then
    {
      echo ""
      echo "# Devcontainer Plugins Loader"
      echo "import importlib.util, os"
      echo "PLUGINS = ['${PLUGIN_NAME}']"
      echo "PLUGINS_CONFIG = {'${PLUGIN_NAME}': {}}"
      echo "_pc_path = '${PLUGIN_WS_DIR}/.devcontainer/config/plugin-config.py'"
      echo "if os.path.isfile(_pc_path):"
      echo "    _spec = importlib.util.spec_from_file_location('workspace_plugin_config', _pc_path)"
      echo "    _mod = importlib.util.module_from_spec(_spec)"
      echo "    try:"
      echo "        _spec.loader.exec_module(_mod)"
      echo "        PLUGINS = getattr(_mod, 'PLUGINS', PLUGINS)"
      echo "        PLUGINS_CONFIG = getattr(_mod, 'PLUGINS_CONFIG', PLUGINS_CONFIG)"
      echo "    except Exception as e:"
      echo "        print(f'⚠️  Failed to load plugin-config.py: {e}')"
      echo "else:"
      echo "    print('ℹ️ plugin-config.py not found; using defaults')"

      echo "# Import optional extra NetBox configuration (uppercase settings)"
      echo "_xc_path = '${PLUGIN_WS_DIR}/.devcontainer/config/extra-configuration.py'"
      echo "if os.path.isfile(_xc_path):"
      echo "    _xc_spec = importlib.util.spec_from_file_location('workspace_extra_configuration', _xc_path)"
      echo "    _xc_mod = importlib.util.module_from_spec(_xc_spec)"
      echo "    try:"
      echo "        _xc_spec.loader.exec_module(_xc_mod)"
      echo "        for _name in dir(_xc_mod):"
      echo "            if _name.isupper():"
      echo "                globals()[_name] = getattr(_xc_mod, _name)"
      echo "    except Exception as e:"
      echo "        print(f'⚠️  Failed to apply extra-configuration.py: {e}')"

      echo "# Import Codespaces configuration when applicable (uppercase settings)"
      echo "_cs_path = '${PLUGIN_WS_DIR}/.devcontainer/config/codespaces-configuration.py'"
      echo "if os.environ.get('CODESPACES') == 'true' and os.path.isfile(_cs_path):"
      echo "    _cs_spec = importlib.util.spec_from_file_location('workspace_codespaces_configuration', _cs_path)"
      echo "    _cs_mod = importlib.util.module_from_spec(_cs_spec)"
      echo "    try:"
      echo "        _cs_spec.loader.exec_module(_cs_mod)"
      echo "        for _name in dir(_cs_mod):"
      echo "            if _name.isupper():"
      echo "                globals()[_name] = getattr(_cs_mod, _name)"
      echo "    except Exception as e:"
      echo "        print(f'⚠️  Failed to apply codespaces-configuration.py: {e}')"

      echo "if 'SECRET_KEY' not in globals() or not SECRET_KEY:"
      echo "    SECRET_KEY = os.environ.get('SECRET_KEY', 'dummydummydummydummydummydummydummydummydummydummydummydummy')"
    } >> "$CONF_FILE"
  fi
  echo "✅ Plugin configuration injected into NetBox settings"
fi

# Run migrations
cd /opt/netbox/netbox
export DEBUG="${DEBUG:-True}"

echo "🗃️  Applying database migrations..."
python manage.py migrate 2>&1 | grep -E "(Operations to perform|Running migrations|Apply all migrations|No migrations to apply|\s+Applying|\s+OK)" || true

echo "🔐 Creating superuser (if not exists)..."
python manage.py shell -c "
from django.contrib.auth import get_user_model
User = get_user_model()
username = '${SUPERUSER_NAME:-admin}'
email = '${SUPERUSER_EMAIL:-admin@example.com}'
password = '${SUPERUSER_PASSWORD:-admin}'
if not User.objects.filter(username=username).exists():
    User.objects.create_superuser(username, email, password)
    print(f'Created superuser: {username}/{password}')
else:
    print(f'Superuser {username} already exists')
" 2>/dev/null || true

echo "📊 Collecting static files..."
python manage.py collectstatic --noinput >/dev/null 2>&1 || true

echo "📂 Loading sample data from contrib/..."
LIBRENMS_CONTRIB="$PLUGIN_WS_DIR/../netbox-librenms-plugin/.devcontainer/scripts/load-sample-data.py"
INR_CONTRIB="$PLUGIN_WS_DIR/.devcontainer/scripts/load-sample-data.py"
if [ -f "$LIBRENMS_CONTRIB" ]; then
  python manage.py shell < "$LIBRENMS_CONTRIB" 2>/dev/null | grep -E "(Loading|✓|✅|created|updated|skipped|⚠️)" || true
fi
if [ -f "$INR_CONTRIB" ]; then
  python manage.py shell < "$INR_CONTRIB" 2>/dev/null | grep -E "(Loading|✓|✅|created|updated|skipped|⚠️)" || true
fi

# Pre-commit hooks
cd "$PLUGIN_WS_DIR"
git config --global --add safe.directory "$PLUGIN_WS_DIR"
pre-commit install --install-hooks 2>/dev/null || echo "⚠️  Pre-commit hook installation failed"

# Ensure scripts are executable
chmod +x "$PLUGIN_WS_DIR/.devcontainer/scripts/"*.sh 2>/dev/null || true

# Load aliases into .bashrc
BASHRC_SENTINEL="# NetBox ${PLUGIN_DISPLAY} — source aliases"
if ! grep -qF "$BASHRC_SENTINEL" ~/.bashrc 2>/dev/null; then
  cat >> ~/.bashrc << EOF
$BASHRC_SENTINEL
source "$PLUGIN_WS_DIR/.devcontainer/scripts/load-aliases.sh"
bash "$PLUGIN_WS_DIR/.devcontainer/scripts/welcome.sh"
EOF
fi

# Fix Git remote URLs for dev container compatibility
CURRENT_REMOTE=$(git remote get-url origin 2>/dev/null || echo "")
if [[ "$CURRENT_REMOTE" == git@github.com:* ]]; then
  HTTPS_URL=$(echo "$CURRENT_REMOTE" | sed 's|git@github.com:|https://github.com/|')
  git remote set-url origin "$HTTPS_URL"
  echo "✅ Converted Git remote from SSH to HTTPS: $HTTPS_URL"
fi

# Validation
cd /opt/netbox/netbox
if python -c "import ${PLUGIN_NAME}" 2>/dev/null; then
  echo "✅ ${PLUGIN_NAME} is properly installed and importable"
else
  echo "⚠️  Warning: ${PLUGIN_NAME} may not be properly installed"
fi

echo ""
echo "🚀 NetBox ${PLUGIN_DISPLAY} Plugin Dev Environment Ready!"
