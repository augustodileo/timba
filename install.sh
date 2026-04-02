#!/usr/bin/env bash
# Install the timba binary.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/augustodileo/timba/main/install.sh | sh
#   ./install.sh                    # from cloned repo
#   ./install.sh --uninstall        # remove binary
#   VERSION=v3.2.0 ./install.sh     # pin version

set -euo pipefail

REPO="augustodileo/timba"
BIN_DIR="${TIMBA_INSTALL:-${XDG_BIN_HOME:-$HOME/.local/bin}}"
NEEDS_RELOAD=""

# ── Help ───────────────────────────────────────────────────────

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    echo "Usage: ./install.sh [--uninstall]"
    echo ""
    echo "Install the timba binary to ~/.local/bin/"
    echo ""
    echo "Environment variables:"
    echo "  VERSION          Pin to a specific release (e.g. v3.2.0)"
    echo "  TIMBA_INSTALL    Custom install directory (default: ~/.local/bin)"
    exit 0
fi

# ── Uninstall ──────────────────────────────────────────────────

if [ "${1:-}" = "--uninstall" ]; then
    if [ -f "$BIN_DIR/timba" ]; then
        rm -f "$BIN_DIR/timba"
        echo "Removed $BIN_DIR/timba"
    else
        echo "timba not found in $BIN_DIR"
    fi
    exit 0
fi

# ── Detect platform ───────────────────────────────────────────

OS="$(uname -s)"
ARCH="$(uname -m)"

case "$OS" in
    Linux)  OS="linux" ;;
    Darwin) OS="darwin" ;;
    MINGW*|MSYS*|CYGWIN*)
        echo "Error: this installer is for Linux/macOS." >&2
        echo "  Download the Windows binary from: https://github.com/$REPO/releases" >&2
        exit 1 ;;
    *)      echo "Error: unsupported OS: $OS" >&2; exit 1 ;;
esac

case "$ARCH" in
    x86_64|amd64)   ARCH="x64" ;;
    aarch64|arm64)  ARCH="arm64" ;;
    *)              echo "Error: unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

# macOS: detect Rosetta (uname -m reports x86_64 under Rosetta on ARM Macs)
if [ "$OS" = "darwin" ] && [ "$ARCH" = "x64" ]; then
    if sysctl -n sysctl.proc_translated 2>/dev/null | grep -q 1; then
        ARCH="arm64"
    fi
fi

TARGET="${OS}-${ARCH}"

# ── Resolve version ───────────────────────────────────────────

if [ -z "${VERSION:-}" ]; then
    echo "Fetching latest release..."
    VERSION=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
        | grep '"tag_name"' | cut -d'"' -f4)
    if [ -z "$VERSION" ]; then
        echo "Error: could not determine latest version." >&2
        echo "  Check https://github.com/$REPO/releases" >&2
        exit 1
    fi
fi

# Archive uses PEP 440 version (no v prefix), tag keeps the v prefix
PKG_VERSION="${VERSION#v}"
ARCHIVE="timba-${PKG_VERSION}-${TARGET}.tar.gz"
URL="https://github.com/$REPO/releases/download/${VERSION}/${ARCHIVE}"

echo "Installing timba $VERSION ($TARGET)..."

# ── Download ──────────────────────────────────────────────────

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

if ! curl -fsSL -o "$TMPDIR/$ARCHIVE" "$URL"; then
    echo "" >&2
    echo "Error: download failed." >&2
    echo "  URL: $URL" >&2
    echo "  Check that $VERSION has a binary for $TARGET at:" >&2
    echo "  https://github.com/$REPO/releases/tag/$VERSION" >&2
    exit 1
fi

# ── Verify checksum ───────────────────────────────────────────

if curl -fsSL -o "$TMPDIR/${ARCHIVE}.sha256" "${URL}.sha256" 2>/dev/null; then
    cd "$TMPDIR"
    if command -v sha256sum &>/dev/null; then
        sha256sum -c "${ARCHIVE}.sha256" --quiet
    elif command -v shasum &>/dev/null; then
        shasum -a 256 -c "${ARCHIVE}.sha256" --quiet
    fi
    cd - > /dev/null
else
    echo "  Warning: checksum file not found, skipping verification"
fi

# ── Install ───────────────────────────────────────────────────

tar xzf "$TMPDIR/$ARCHIVE" -C "$TMPDIR"
mkdir -p "$BIN_DIR"
mv "$TMPDIR/timba" "$BIN_DIR/timba"
chmod +x "$BIN_DIR/timba"

# ── Home directory + config ────────────────────────────────────

TIMBA_HOME="${TIMBA_HOME:-$HOME/.timba}"
mkdir -p "$TIMBA_HOME"
# config.yaml is included in the archive alongside the binary
if [ -f "$TMPDIR/config.yaml" ]; then
    if [ ! -f "$TIMBA_HOME/config.yaml" ]; then
        mv "$TMPDIR/config.yaml" "$TIMBA_HOME/config.yaml"
        echo "  Config: $TIMBA_HOME/config.yaml"
    else
        echo "  Config: kept existing $TIMBA_HOME/config.yaml"
    fi
fi

# ── PATH ──────────────────────────────────────────────────────

if ! echo "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
    SHELL_NAME="$(basename "${SHELL:-bash}")"
    case "$SHELL_NAME" in
        zsh)  RC_FILE="$HOME/.zshrc" ;;
        bash) RC_FILE="$HOME/.bashrc" ;;
        *)    RC_FILE="$HOME/.profile" ;;
    esac

    if ! grep -qF "$BIN_DIR" "$RC_FILE" 2>/dev/null; then
        printf '\n# Added by timba\nexport PATH="%s:$PATH"\n' "$BIN_DIR" >> "$RC_FILE"
        NEEDS_RELOAD="$RC_FILE"
    fi
fi

# ── Summary ───────────────────────────────────────────────────

echo ""
echo "Installed timba $VERSION"
echo "  Binary: $BIN_DIR/timba"
echo "  Config: $TIMBA_HOME/config.yaml"
echo ""
echo "  Get started:"
echo "    timba start              set up credentials and start the bot"
echo "    timba status             check bot status"
echo "    timba stop               stop the bot"
echo ""
echo "  Data: $TIMBA_HOME/"

if [ -n "$NEEDS_RELOAD" ]; then
    echo ""
    echo "  Restart your terminal or run: source $NEEDS_RELOAD"
fi
