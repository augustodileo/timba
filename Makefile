# Timba — Polymarket crypto trading bot
#
# Usage:
#   make              build binary (runs tests first)
#   make test         run tests
#   make lint         ruff + bandit
#   make audit        pip-audit + secret scan
#   make build        build binary locally (runs tests first)
#   make install      build + install to ~/.local/bin/ + config to ~/.timba/
#   make docker       build Docker image
#   make docker-test  smoke test + structure test on Docker image
#   make package      build + tar.gz for release (CI uses this)
#   make clean        remove build artifacts

REGISTRY := $(shell git remote get-url origin 2>/dev/null | sed 's|.*github.com[:/]||;s|\.git$$||' | xargs -I{} echo "ghcr.io/{}")

# Version from hatch-vcs (the single source of truth).
_version = $(shell uv run python -c "from timba._version import __version__; print(__version__)" 2>/dev/null || echo dev)

.PHONY: all sync lint audit test build install docker docker-image docker-test package clean

all: build

# ── Sync ─────────────────────────────────────────────────────

sync:
	@uv sync --all-extras -q

# ── Lint ─────────────────────────────────────────────────────

lint: sync
	uv run ruff check src/ tests/
	uv run bandit -r src/timba/ -c pyproject.toml -q

# ── Audit ────────────────────────────────────────────────────

audit: sync
	.github/hooks/pre-commit all
	uv run pip-audit --skip-editable

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

docker-image:
	@echo "$(REGISTRY):$(_version)"

docker: sync
	docker build --build-arg VERSION=$(_version) -t $(REGISTRY):$(_version) .
	@echo ""
	@echo "Built: $(REGISTRY):$(_version)"

docker-test: sync
	docker run --rm $(REGISTRY):$(_version) --version
	docker run --rm --entrypoint "" $(REGISTRY):$(_version) sh -c '\
		test -f /app/timba && \
		test -f /app/config.yaml && \
		whoami | grep -q bot && \
		echo "Structure tests passed"'

# ── Package (CI) ──────────────────────────────────────────────

package: build
	cp config.yaml dist/config.yaml
	cd dist && tar czf "timba-$(_version)-$${TARGET:-local}.tar.gz" timba config.yaml
	cd dist && shasum -a 256 "timba-$(_version)-$${TARGET:-local}.tar.gz" > "timba-$(_version)-$${TARGET:-local}.tar.gz.sha256"
	@echo "Packaged: dist/timba-$(_version)-$${TARGET:-local}.tar.gz"

# ── Clean ─────────────────────────────────────────────────────

clean:
	rm -rf dist/ build/ *.spec
