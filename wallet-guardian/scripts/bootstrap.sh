#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required: https://docs.astral.sh/uv/" >&2
  exit 1
fi
uv sync --dev
echo "Run: uv run guardian doctor --json"
