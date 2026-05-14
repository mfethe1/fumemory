from __future__ import annotations

from dataclasses import dataclass

import requests


SEARCH_URL = "http://localhost:8000/search"
API_KEY = "memu-dev-key"


@dataclass
class _Response:
    status_code: int = 200
    text: str = '{"results": []}'


def search_for_test_query() -> requests.Response:
    return requests.post(
        SEARCH_URL,
        headers={"X-API-Key": API_KEY},
        json={"query": "test query", "limit": 2},
    )


def test_search_smoke_request_shape(monkeypatch):
    recorded_request = {}

    def fake_post(url, *, headers, json):
        recorded_request["url"] = url
        recorded_request["headers"] = headers
        recorded_request["json"] = json
        return _Response()

    monkeypatch.setattr(requests, "post", fake_post)

    response = search_for_test_query()

    assert response.status_code == 200
    assert recorded_request == {
        "url": "http://localhost:8000/search",
        "headers": {"X-API-Key": "memu-dev-key"},
        "json": {"query": "test query", "limit": 2},
    }
