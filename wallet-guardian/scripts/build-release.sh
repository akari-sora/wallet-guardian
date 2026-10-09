#!/bin/sh
# Build dist/*.whl, dist/*.tar.gz, dist/constraints.txt, and dist/SHA256SUMS.
# uv tool install does not read uv.lock; install with the constraints file.
set -eu
cd "$(dirname "$0")/.."
exec python tools/build_release.py "$@"
