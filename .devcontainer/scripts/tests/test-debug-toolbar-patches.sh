#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
#
# Exercises the debug-toolbar patch blocks in start-netbox.sh against real fixture
# files. The blocks are extracted from the real script so this cannot drift from it.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="$SCRIPT_DIR/../start-netbox.sh"
WORK="$(mktemp -d)"
trap 'chmod -R u+w "$WORK" 2>/dev/null; rm -rf "$WORK"' EXIT

FAILURES=0
ok() { echo "  ok: $1"; }
fail() { echo "  FAIL: $1" >&2; FAILURES=$((FAILURES + 1)); }

# Both patch blocks live between the NETBOX_SETTINGS assignment and the Codespaces block.
awk '/^NETBOX_SETTINGS=/{f=1} /^# Detect Codespaces/{f=0} f' "$TARGET" > "$WORK/blocks.sh"
for marker in NETBOX_SETTINGS= NETBOX_LOCAL_SETTINGS= USE_SHADOW_DOM DEBUG_TOOLBAR_PANELS; do
  grep -q "$marker" "$WORK/blocks.sh" || { echo "FAIL: extraction missed $marker" >&2; exit 1; }
done

run_blocks() {  # $1=settings.py  $2=local_settings.py -> stdout+stderr of the blocks
  sed -e "s|^NETBOX_SETTINGS=.*|NETBOX_SETTINGS=\"$1\"|" \
      -e "s|^NETBOX_LOCAL_SETTINGS=.*|NETBOX_LOCAL_SETTINGS=\"$2\"|" \
      "$WORK/blocks.sh" > "$WORK/run.sh"
  bash "$WORK/run.sh" 2>&1
}

mk_settings() {
  cat > "$1" <<'EOF'
DEBUG_TOOLBAR_CONFIG = {
    'SHOW_TOOLBAR_CALLBACK': 'utilities.debug.show_toolbar',
}
EOF
}

echo "case: anchor present -> both patches applied, files stay valid Python"
S="$WORK/settings.py"; L="$WORK/local_settings.py"; mk_settings "$S"; : > "$L"
run_blocks "$S" "$L" > /dev/null
grep -q "'USE_SHADOW_DOM': False," "$S" && ok "USE_SHADOW_DOM injected" || fail "USE_SHADOW_DOM not injected"
grep -q "DEBUG_TOOLBAR_PANELS" "$L" && ok "ProfilingPanel dropped" || fail "ProfilingPanel not dropped"
python3 -c "import ast; ast.parse(open('$S').read())" 2>/dev/null && ok "settings.py parses" || fail "settings.py broken"
python3 -c "import ast; ast.parse(open('$L').read())" 2>/dev/null && ok "local_settings.py parses" || fail "local_settings.py broken"

echo "case: re-run is idempotent"
run_blocks "$S" "$L" > /dev/null
[ "$(grep -c 'USE_SHADOW_DOM' "$S")" -eq 1 ] && ok "settings.py patched once" || fail "settings.py patched repeatedly"
[ "$(grep -c 'DEBUG_TOOLBAR_PANELS' "$L")" -eq 1 ] && ok "local_settings.py appended once" || fail "local_settings.py appended repeatedly"

echo "case: anchor missing -> warns and leaves the file untouched"
S3="$WORK/no_anchor.py"; L3="$WORK/l3.py"; echo "DEBUG_TOOLBAR_CONFIG = {}" > "$S3"; : > "$L3"
BEFORE="$(md5sum < "$S3")"
OUT="$(run_blocks "$S3" "$L3")"
echo "$OUT" | grep -q "anchor not found" && ok "anchor warning emitted" || fail "no anchor warning"
[ "$BEFORE" = "$(md5sum < "$S3")" ] && ok "file untouched" || fail "file mutated"

echo "case: files missing -> warns instead of skipping silently"
OUT="$(run_blocks "$WORK/absent_a.py" "$WORK/absent_b.py")"
echo "$OUT" | grep -q "USE_SHADOW_DOM patch skipped: .*not found" && ok "settings.py warning" || fail "no settings.py warning"
echo "$OUT" | grep -q "ProfilingPanel patch skipped: .*not found" && ok "local_settings.py warning" || fail "no local_settings.py warning"

echo "case: unwritable target -> warns, never reports success"
RO="$WORK/ro"; mkdir -p "$RO"; mk_settings "$RO/settings.py"; : > "$RO/local.py"
chmod a-w "$RO" "$RO/settings.py" "$RO/local.py"
OUT="$(run_blocks "$RO/settings.py" "$RO/local.py")"
chmod -R u+w "$RO"
echo "$OUT" | grep -q "🔧 Patched" && fail "claimed success on unwritable file" || ok "no false success"
[ "$(echo "$OUT" | grep -c "patch failed")" -eq 2 ] && ok "both write failures warned" || fail "write failures not warned"

if [ "$FAILURES" -ne 0 ]; then
  echo "$FAILURES check(s) failed" >&2
  exit 1
fi
echo "all debug-toolbar patch checks passed"
