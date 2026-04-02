#!/usr/bin/env bash
# Build timba binary with PyInstaller.
#
# Version comes from hatch-vcs (reads git tags via setuptools-scm).
# When called from Makefile: deps already synced, _version.py exists.
# When called standalone: syncs deps first.
#
# Usage:
#   make build                  # via Makefile (recommended)
#   scripts/build.sh            # standalone

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

# Sync deps (fast no-op if already done by Make)
uv sync --all-extras -q
uv pip install pyinstaller -q 2>/dev/null

# Read version from hatch-vcs generated _version.py
VERSION=$(uv run python -c "from timba._version import __version__; print(__version__)" 2>/dev/null || echo dev)
echo "Building timba ${VERSION}..."

# OS-specific: PyInstaller --add-data separator and binary name
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
    SEP=";"
    BIN="dist/timba.exe"
else
    SEP=":"
    BIN="dist/timba"
fi

uv run pyinstaller --onefile --name timba -y \
    --collect-all timba \
    --collect-all pyfiglet \
    --hidden-import timba.strategies.favorite \
    --add-data "config.yaml${SEP}." \
    --add-data ".env.example${SEP}." \
    src/timba/cli.py 2>&1 | grep -E "^[0-9]+ INFO: (Building|Build complete)" || true

# Smoke test
"$BIN" --version > /dev/null

SIZE=$(ls -lh "$BIN" | awk '{print $5}')
echo ""
echo "Built: $BIN ($SIZE)"
