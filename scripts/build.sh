#!/usr/bin/env bash
# Build timba binary with PyInstaller.
#
# Usage:
#   make build                  # via Makefile (recommended)
#   scripts/build.sh            # standalone (resolves version + syncs)

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

# Version: Makefile exports these, resolve if running standalone
if [ -z "${VERSION:-}" ]; then
    VERSION="$(git describe --tags --always 2>/dev/null || echo 0.0.0.dev0)"
fi
export SETUPTOOLS_SCM_PRETEND_VERSION="${SETUPTOOLS_SCM_PRETEND_VERSION:-$VERSION}"

echo "Building timba ${VERSION}..."

# Sync + install PyInstaller (fast no-op if already done by Make)
uv sync --all-extras -q
uv pip install pyinstaller -q 2>/dev/null

uv run pyinstaller --onefile --name timba -y \
    --collect-all timba \
    --collect-all pyfiglet \
    --hidden-import timba.strategies.favorite \
    --add-data "config.yaml:." \
    --add-data ".env.example:." \
    src/timba/cli.py 2>&1 | grep -E "^[0-9]+ INFO: (Building|Build complete)" || true

# Smoke test
./dist/timba --version > /dev/null

SIZE=$(ls -lh dist/timba | awk '{print $5}')
echo ""
echo "Built: dist/timba ($SIZE)"
