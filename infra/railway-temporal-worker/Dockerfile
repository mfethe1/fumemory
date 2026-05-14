FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG FUMEMORY_REPO=https://github.com/mfethe1/fumemory.git
ARG FUMEMORY_REF=main

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# Invalidate Docker cache when main moves, while still allowing FUMEMORY_REF
# to pin a branch/tag/SHA for controlled rollouts.
ADD https://api.github.com/repos/mfethe1/fumemory/git/refs/heads/main /tmp/fumemory-main-ref.json

RUN git clone --depth 1 --branch "$FUMEMORY_REF" "$FUMEMORY_REPO" /src

WORKDIR /src

RUN python -m pip install --no-cache-dir \
    "fastapi>=0.100" \
    "uvicorn>=0.20" \
    "asyncpg>=0.28" \
    "httpx>=0.24" \
    "pydantic>=2.0" \
    "nats-py>=2.7" \
    "docker>=7" \
    "fastembed>=0.5" \
    "temporalio==1.23.0"

CMD ["python", "-m", "memu.temporal_worker.worker"]
