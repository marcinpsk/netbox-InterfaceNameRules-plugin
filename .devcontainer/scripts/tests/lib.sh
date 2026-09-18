#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

# Populate COMPOSE_FILES with the `-f` arguments a real recreate would use.
#
# The override is untracked and per-developer, but Docker applies it to every recreate. A check that
# renders only the base file cannot see a value the override replaces, so it would pass while the
# container it protects comes up wrong.
compose_file_args() {
  local root="$1"
  local override="$root/.devcontainer/docker-compose.override.yml"

  COMPOSE_FILES=(-f "$root/.devcontainer/docker-compose.yml")
  if [ -f "$override" ]; then
    COMPOSE_FILES+=(-f "$override")
  fi
}
