FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml .
COPY README.md .
COPY memu/ memu/
RUN python -m pip install --no-cache-dir \
    "fastapi>=0.100" \
    "uvicorn>=0.20" \
    "asyncpg>=0.28" \
    "httpx>=0.24" \
    "pydantic>=2.0" \
    "nats-py>=2.7" \
    "docker>=7" \
    "fastembed>=0.5" \
    "temporalio>=1.7.0" \
    "PyNaCl>=1.5.0"

EXPOSE 8000
CMD ["python", "-m", "memu.api"]
