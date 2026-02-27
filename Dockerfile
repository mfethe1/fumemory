FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml .
COPY memu/ memu/
COPY README.md .
RUN pip install --no-cache-dir fastapi>=0.100 uvicorn>=0.20 asyncpg>=0.28 httpx>=0.24 pydantic>=2.0 nats-py>=2.7 docker>=7 fastembed>=0.2.0

EXPOSE 8000
CMD ["python", "-m", "memu.api"]
