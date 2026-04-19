"""Manual smoke script for the /search endpoint.

Run directly (``python tests/test_memu_search.py``) against a live memU API.
Not a pytest test — the body runs under ``__main__`` so collection is safe.
"""

import os

import requests


def main() -> None:
    res = requests.post(
        "http://localhost:8000/search",
        headers={"X-API-Key": os.environ.get("MEMU_API_KEY", "memu-dev-key")},
        json={"query": "test query", "limit": 2},
    )
    print("Status:", res.status_code)
    print("Response:", res.text[:200])


if __name__ == "__main__":
    main()
