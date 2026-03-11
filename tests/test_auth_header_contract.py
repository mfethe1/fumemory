import pytest

from memu import api
from memu import temporal_routes


@pytest.mark.asyncio
async def test_verify_api_key_accepts_x_memu_key_value_directly():
    api.MEMU_API_KEY = "test-key"
    auth = await api.verify_api_key(memu_key="test-key", legacy_key=None)
    assert str(auth) == "test-key"


@pytest.mark.asyncio
async def test_verify_api_key_accepts_legacy_x_api_key_value_directly():
    api.MEMU_API_KEY = "test-key"
    auth = await api.verify_api_key(memu_key=None, legacy_key="test-key")
    assert str(auth) == "test-key"


@pytest.mark.asyncio
async def test_temporal_verify_api_key_accepts_both_headers():
    temporal_routes.MEMU_API_KEY = "test-key"
    assert await temporal_routes.verify_api_key(memu_key="test-key", legacy_key=None) == "test-key"
    assert await temporal_routes.verify_api_key(memu_key=None, legacy_key="test-key") == "test-key"
