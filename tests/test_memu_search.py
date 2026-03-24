import requests
import os

res = requests.post(
    "http://localhost:8000/search",
    headers={"X-API-Key": "memu-dev-key"},
    json={"query": "test query", "limit": 2}
)
print("Status:", res.status_code)
print("Response:", res.text[:200])
