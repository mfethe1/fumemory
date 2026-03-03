import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from memu.policy.opa_client import OPAClient

@pytest.mark.asyncio
async def test_opa_client_allow():
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"result": {"allow": True, "reason": "ok"}}
        mock_post.return_value = mock_resp
        
        allowed, reason = await OPAClient.evaluate("memu/warden/allow", {"action": "spawn", "role": "admin", "active_containers": 1, "max_containers": 5})
        assert allowed is True
        assert reason == "Allowed"

@pytest.mark.asyncio
async def test_opa_client_deny():
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"result": {"allow": False, "reason": "budget exceeded"}}
        mock_post.return_value = mock_resp
        
        allowed, reason = await OPAClient.evaluate("memu/warden/allow", {"action": "spawn", "compute_budget": 2000})
        assert allowed is False
        assert reason == "budget exceeded"

@pytest.mark.asyncio
async def test_opa_client_fail_open():
    with patch("httpx.AsyncClient.post", side_effect=Exception("Connection Refused")):
        allowed, reason = await OPAClient.evaluate("memu/warden/allow", {})
        assert allowed is True
        assert "fail-open" in reason

