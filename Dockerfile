FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml .
COPY memu/ memu/
RUN pip install --no-cache-dir .

EXPOSE 8000
CMD ["uvicorn", "memu.api:app", "--host", "0.0.0.0", "--port", "8000"]
