#!/bin/bash
set -e

PLUGIN_NAME="netbox_interface_name_rules"
PLUGIN_DISPLAY="Interface Name Rules"
LIBRENMS_PLUGIN_DIR="/workspaces/netbox-librenms-plugin"

echo "🚀 Setting up NetBox ${PLUGIN_DISPLAY} Plugin development environment..."
echo "📍 Current working directory: $(pwd)"
NETBOX_VERSION=${NETBOX_VERSION:-"latest"}
echo "📦 Using NetBox Docker image: netboxcommunity/netbox:${NETBOX_VERSION}"

# Detect this plugin's workspace
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

# Proxy/CA setup (same as librenms plugin)
if [ -n "$HTTP_PROXY" ] || [ -n "$HTTPS_PROXY" ]; then
  echo "🌐 Configuring proxy settings..."
  [ -n "$HTTP_PROXY" ] && echo "Acquire::http::Proxy \"$HTTP_PROXY\";" > /etc/apt/apt.conf.d/80proxy
  [ -n "$HTTPS_PROXY" ] && echo "Acquire::https::Proxy \"$HTTPS_PROXY\";" >> /etc/apt/apt.conf.d/80proxy
  export HTTP_PROXY HTTPS_PROXY http_proxy https_proxy NO_PROXY no_proxy

  # Try CA bundle from librenms plugin workspace (shared proxy setup)
  CA_BUNDLE_SRC="${LIBRENMS_PLUGIN_DIR}/ca-bundle.crt"
  if [ -f "$CA_BUNDLE_SRC" ]; then
    echo "🔐 Installing custom CA certificate..."
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

if command -v uv >/dev/null 2>&1; then PIP_CMD="uv pip"; else PIP_CMD="pip"; fi

echo "🔧 Installing development dependencies..."
apt-get update -qq
apt-get install -y -qq net-tools git
$PIP_CMD install pytest pytest-django ruff pre-commit

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
$PIP_CMD install -e .
echo "✅ Installed $PLUGIN_NAME in editable mode"

# Also install netbox-librenms-plugin if available (for co-development)
if [ -d "$LIBRENMS_PLUGIN_DIR" ] && [ -f "$LIBRENMS_PLUGIN_DIR/pyproject.toml" ]; then
  echo "📦 Installing companion: netbox-librenms-plugin (editable)..."
  cd "$LIBRENMS_PLUGIN_DIR"
  $PIP_CMD install -e .
  echo "✅ Installed netbox-librenms-plugin for co-development"
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

# Pre-commit hooks
cd "$PLUGIN_WS_DIR"
git config --global --add safe.directory "$PLUGIN_WS_DIR"
pre-commit install --install-hooks 2>/dev/null || echo "⚠️  Pre-commit hook installation failed"

# Validation
cd /opt/netbox/netbox
if python -c "import ${PLUGIN_NAME}" 2>/dev/null; then
  echo "✅ ${PLUGIN_NAME} is properly installed and importable"
else
  echo "⚠️  Warning: ${PLUGIN_NAME} may not be properly installed"
fi

echo ""
echo "🚀 NetBox ${PLUGIN_DISPLAY} Plugin Dev Environment Ready!"
