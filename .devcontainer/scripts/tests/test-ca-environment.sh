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

# Compare the rendered value as a string. A grep pattern would read every "." in a certificate
# path as a wildcard, so a wrong path such as ca-certificatesXcrt would satisfy the check.
for variable in REQUESTS_CA_BUNDLE SSL_CERT_FILE CURL_CA_BUNDLE; do
  value="$(awk -v key="$variable:" '$1 == key { print $2; exit }' <<< "$config")"
  value="${value%\"}"
  value="${value#\"}"
  if [ "$value" != "$CONTAINER_CA_BUNDLE" ]; then
    echo "FAIL: $variable is '$value', not the container trust store $CONTAINER_CA_BUNDLE" >&2
    exit 1
  fi
done

if grep -qF "$HOST_CA_BUNDLE" <<< "$config"; then
  echo "FAIL: a host CA path leaked into the container configuration" >&2
  exit 1
fi

echo "Container CA environment check passed"
