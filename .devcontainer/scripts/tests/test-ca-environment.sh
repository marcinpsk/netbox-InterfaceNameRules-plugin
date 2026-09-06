#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
CONTAINER_CA_BUNDLE="/etc/ssl/certs/ca-certificates.crt"
HOST_CA_BUNDLE="/host-only/ca.pem"

config="$({
  REQUESTS_CA_BUNDLE="$HOST_CA_BUNDLE" \
  SSL_CERT_FILE="$HOST_CA_BUNDLE" \
  CURL_CA_BUNDLE="$HOST_CA_BUNDLE" \
    docker compose -f "$REPO_ROOT/.devcontainer/docker-compose.yml" config
})"

for variable in REQUESTS_CA_BUNDLE SSL_CERT_FILE CURL_CA_BUNDLE; do
  if ! grep -q "^[[:space:]]*$variable: $CONTAINER_CA_BUNDLE$" <<< "$config"; then
    echo "FAIL: $variable does not use the container trust store" >&2
    exit 1
  fi
done

if grep -q "$HOST_CA_BUNDLE" <<< "$config"; then
  echo "FAIL: a host CA path leaked into the container configuration" >&2
  exit 1
fi

echo "Container CA environment check passed"
