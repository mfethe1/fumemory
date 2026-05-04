import pytest

from memu import api
from memu import embeddings_client


class _DisconnectedNATSCluster:
    @property
    def active_connection(self):
        raise ConnectionError("No NATS connections available")


@pytest.mark.asyncio
async def test_get_embedding_degrades_when_nats_cluster_disconnected(monkeypatch):
    captured = {}

    async def fake_embedding_client(text, *, nc=None):
        captured["text"] = text
        captured["nc"] = nc
        return None

    monkeypatch.setattr(api, "_nats_cluster", _DisconnectedNATSCluster())
    monkeypatch.setattr(embeddings_client, "get_embedding", fake_embedding_client)

    result = await api.get_embedding("write should not fail")

    assert result is None
    assert captured == {"text": "write should not fail", "nc": None}
