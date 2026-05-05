from __future__ import annotations

import importlib

import pytest


class _EmbeddingResponse:
    status_code = 200
    text = '{"data":[{"embedding":[0.1,0.2,0.3]}]}'

    def json(self):
        return {"data": [{"embedding": [0.1, 0.2, 0.3]}]}


class _CapturingAsyncClient:
    requests: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json, headers):
        self.requests.append({"url": url, "json": json, "headers": headers})
        return _EmbeddingResponse()


@pytest.mark.asyncio
async def test_embedding_client_uses_deprecated_base_url_alias(monkeypatch):
    monkeypatch.delenv("EMBEDDING_API_BASE", raising=False)
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://ollama:11434")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("EMBEDDING_MODEL", "test-embedding-model")

    import memu.embeddings_client as embeddings_client

    embeddings_client = importlib.reload(embeddings_client)
    _CapturingAsyncClient.requests = []
    monkeypatch.setattr(embeddings_client.httpx, "AsyncClient", _CapturingAsyncClient)

    embedding = await embeddings_client.get_embedding("contract text")

    assert embedding == [0.1, 0.2, 0.3]
    assert _CapturingAsyncClient.requests[0]["url"] == "http://ollama:11434/v1/embeddings"


@pytest.mark.asyncio
async def test_ollama_embedding_base_can_request_without_openai_key(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_BASE", "http://localhost:11434")
    monkeypatch.delenv("EMBEDDING_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    import memu.embeddings_client as embeddings_client

    embeddings_client = importlib.reload(embeddings_client)
    _CapturingAsyncClient.requests = []
    monkeypatch.setattr(embeddings_client.httpx, "AsyncClient", _CapturingAsyncClient)

    embedding = await embeddings_client.get_embedding("local embedding text")

    assert embedding == [0.1, 0.2, 0.3]
    assert _CapturingAsyncClient.requests[0]["url"] == "http://localhost:11434/v1/embeddings"
    assert "Authorization" not in _CapturingAsyncClient.requests[0]["headers"]


@pytest.mark.asyncio
async def test_non_ollama_remote_base_requires_openai_key(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_BASE", "https://embeddings.example.com")
    monkeypatch.delenv("EMBEDDING_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    import memu.embeddings_client as embeddings_client

    embeddings_client = importlib.reload(embeddings_client)
    _CapturingAsyncClient.requests = []
    monkeypatch.setattr(embeddings_client.httpx, "AsyncClient", _CapturingAsyncClient)

    embedding = await embeddings_client.get_embedding("remote embedding text")

    assert embedding is None
    assert _CapturingAsyncClient.requests == []
