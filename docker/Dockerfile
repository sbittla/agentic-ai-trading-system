# Multi-stage build: one Dockerfile, one target per service (docs/ARCHITECTURE.md §1)
#   base     -> shared Python runtime and core dependencies
#   core     -> trader-core and replay (Alpaca, indicators, agents)
#   news     -> news-sentiment (adds PyTorch + transformers for FinBERT)
#   notifier -> WhatsApp sender and reply poller
#   tools    -> backup and admin jobs (adds postgres client)
#   dev      -> dev container (adds test and lint tools)

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HOME=/app
WORKDIR /app
# /app/data/bars must exist and belong to trader: a new named volume copies this owner
RUN useradd --create-home --home-dir /app --uid 10001 trader && \
    mkdir -p /app/data/bars && chown -R trader:trader /app/data
# Pinned, hashed dependency sets generated with pip-compile (SEC-10)
COPY requirements/base.lock requirements/base.lock
RUN pip install --require-hashes -r requirements/base.lock
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-deps .
USER trader

FROM base AS core
USER root
COPY requirements/core.lock requirements/core.lock
RUN pip install --require-hashes -r requirements/core.lock
USER trader

FROM base AS news
USER root
COPY requirements/news.lock requirements/news.lock
RUN pip install --require-hashes -r requirements/news.lock   # CPU-only torch wheels
USER trader

FROM base AS notifier
USER root
COPY requirements/notifier.lock requirements/notifier.lock
RUN pip install --require-hashes -r requirements/notifier.lock
USER trader

FROM base AS tools
USER root
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*
USER trader

FROM core AS dev
USER root
COPY requirements/dev.lock requirements/dev.lock
RUN pip install --require-hashes -r requirements/dev.lock && \
    apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
USER trader
