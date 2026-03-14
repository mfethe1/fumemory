FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
COPY pyproject.toml .
COPY README.md .
COPY memu/ memu/
RUN python -m pip install --no-cache-dir -r requirements.txt

EXPOSE 8000
CMD ["python", "-m", "memu.api"]
