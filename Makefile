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

VERSION := $(shell git describe --tags --always 2>/dev/null || echo 0.0.0.dev0)
REGISTRY := $(shell git remote get-url origin 2>/dev/null | sed 's|.*github.com[:/]||;s|\.git$$||' | xargs -I{} echo "ghcr.io/{}")
export VERSION
export SETUPTOOLS_SCM_PRETEND_VERSION := $(VERSION)

.PHONY: all sync lint test build install docker package clean

all: build

# ── Sync ─────────────────────────────────────────────────────

sync:
	@uv sync --all-extras -q

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
	@echo "Installed timba $(VERSION)"
	@echo "  Binary: ~/.local/bin/timba"
	@echo "  Config: ~/.timba/config.yaml"
	@echo "  Run:    timba start"

# ── Docker ────────────────────────────────────────────────────

docker:
	docker build --build-arg VERSION=$(VERSION) -t $(REGISTRY):$(VERSION) .
	@echo ""
	@echo "Built: $(REGISTRY):$(VERSION)"
	@echo "  Run: docker run -e POLYMARKET_PRIVATE_KEY=0x... -e POLYMARKET_FUNDER=0x... $(REGISTRY):$(VERSION)"

# ── Package (CI) ──────────────────────────────────────────────

package: build
	cp config.yaml dist/config.yaml
	cd dist && tar czf "timba-$(VERSION)-$${TARGET:-local}.tar.gz" timba config.yaml
	cd dist && shasum -a 256 "timba-$(VERSION)-$${TARGET:-local}.tar.gz" > "timba-$(VERSION)-$${TARGET:-local}.tar.gz.sha256"
	@echo "Packaged: dist/timba-$(VERSION)-$${TARGET:-local}.tar.gz"

# ── Clean ─────────────────────────────────────────────────────

clean:
	rm -rf dist/ build/ *.spec
