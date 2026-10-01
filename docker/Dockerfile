FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install pinned dependencies first for layer caching (SEC-10: hashes in requirements.lock)
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-deps .

# Run as a non-root user
RUN useradd --create-home --uid 10001 trader && mkdir -p /app/data && chown -R trader /app/data
USER trader

HEALTHCHECK --interval=30s --timeout=10s --retries=3 CMD ["trader", "health"]
ENTRYPOINT ["trader", "run"]
