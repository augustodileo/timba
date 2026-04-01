# Timba — Polymarket crypto trading bot
#
# Usage:
#   make              build binary (runs tests first)
#   make test         run tests
#   make build        build binary locally (runs tests first)
#   make install      build + install to ~/.local/bin/ + config to ~/.timba/
#   make docker       build Docker image
#   make package      build + tar.gz for release (CI uses this)
#   make clean        remove build artifacts

REGISTRY := $(shell git remote get-url origin 2>/dev/null | sed 's|.*github.com[:/]||;s|\.git$$||' | xargs -I{} echo "ghcr.io/{}")

# Version from hatch-vcs (the single source of truth).
# Requires uv sync first, so targets that need it call _version.
.PHONY: all sync lint test build install docker package clean

all: build

# ── Sync ─────────────────────────────────────────────────────

sync:
	@uv sync --all-extras -q

# ── Version (after sync, hatch-vcs has generated _version.py) ─

_version = $(shell uv run python -c "from timba._version import __version__; print(__version__)" 2>/dev/null || echo dev)

# ── Lint ─────────────────────────────────────────────────────

lint: sync
	uv run ruff check src/ tests/
	uv run bandit -r src/timba/ -c pyproject.toml -q

# ── Test ─────────────────────────────────────────────────────

test: sync
	uv run pytest tests/ -v --cov=timba --cov-report=term-missing --cov-fail-under=70

# ── Build (local binary) ─────────────────────────────────────

build: test
	scripts/build.sh

# ── Install ───────────────────────────────────────────────────

install: build
	cp dist/timba ~/.local/bin/timba
	chmod +x ~/.local/bin/timba
	@codesign --force --sign - ~/.local/bin/timba 2>/dev/null || true
	mkdir -p ~/.timba
	@[ -f ~/.timba/config.yaml ] || cp config.yaml ~/.timba/config.yaml
	@echo ""
	@echo "Installed timba $(_version)"
	@echo "  Binary: ~/.local/bin/timba"
	@echo "  Config: ~/.timba/config.yaml"
	@echo "  Run:    timba start"

# ── Docker ────────────────────────────────────────────────────
# Docker has no .git, so we pass the version as a build-arg.

docker: sync
	docker build --build-arg VERSION=$(_version) -t $(REGISTRY):$(_version) -t timba:ci .
	@echo ""
	@echo "Built: $(REGISTRY):$(_version)"
	@echo "  Run: docker run -e POLYMARKET_PRIVATE_KEY=0x... -e POLYMARKET_FUNDER=0x... $(REGISTRY):$(_version)"

# ── Package (CI) ──────────────────────────────────────────────

package: build
	cp config.yaml dist/config.yaml
	cd dist && tar czf "timba-$(_version)-$${TARGET:-local}.tar.gz" timba config.yaml
	cd dist && shasum -a 256 "timba-$(_version)-$${TARGET:-local}.tar.gz" > "timba-$(_version)-$${TARGET:-local}.tar.gz.sha256"
	@echo "Packaged: dist/timba-$(_version)-$${TARGET:-local}.tar.gz"

# ── Clean ─────────────────────────────────────────────────────

clean:
	rm -rf dist/ build/ *.spec
