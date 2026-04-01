# Stage 1: Build the binary
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update -qq && apt-get install -y -qq binutils && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock* ./
COPY src/ ./src/
COPY config.yaml .env.example ./
COPY scripts/build.sh ./scripts/build.sh

ARG VERSION=dev
ENV VERSION=$VERSION
ENV SETUPTOOLS_SCM_PRETEND_VERSION=$VERSION
RUN uv sync --all-extras -q
RUN scripts/build.sh

# Stage 2: Minimal runtime image (same glibc as builder)
FROM python:3.12-slim

RUN apt-get update -qq && apt-get install -y -qq curl && rm -rf /var/lib/apt/lists/*
RUN adduser --disabled-password --no-create-home --gecos "" bot

WORKDIR /app
COPY --from=builder /build/dist/timba /app/timba
COPY config.yaml /app/config.yaml

RUN mkdir -p /app/data && chown -R bot:bot /app
VOLUME ["/app/data"]
USER bot

ENV TIMBA_HOME=/app
ENV TIMBA_BIND=0.0.0.0

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8080/api/health || exit 1

ENTRYPOINT ["/app/timba"]
CMD ["start"]
