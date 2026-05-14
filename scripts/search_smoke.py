"""Manual smoke script: POST /search against a locally running memU API.

Run with a live server on http://localhost:8000:

    python3 scripts/search_smoke.py
"""
import requests


def main() -> None:
    res = requests.post(
        "http://localhost:8000/search",
        headers={"X-API-Key": "memu-dev-key"},
        json={"query": "test query", "limit": 2},
    )
    print("Status:", res.status_code)
    print("Response:", res.text[:200])


if __name__ == "__main__":
    main()
