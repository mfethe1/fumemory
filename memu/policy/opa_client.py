from __future__ import annotations

import logging
import os
from typing import Any
from typing import Dict
from typing import Tuple

import httpx

logger = logging.getLogger(__name__)


class OPAClient:
    @classmethod
    async def evaluate(cls, policy_path: str, input_data: Dict[str, Any]) -> Tuple[bool, str]:
        opa_url = os.environ.get("OPA_URL", "http://localhost:8181")
        url = f"{opa_url}/v1/data/{policy_path}"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json={"input": input_data}, timeout=2.0)
                if response.status_code == 200:
                    result = response.json().get("result", {})
                    # Rego package memu.warden defines 'allow'.
                    # /v1/data/memu/warden returns {"result": {"allow": true, "reason": "..."}}
                    # /v1/data/memu/warden/allow returns {"result": true}
                    if isinstance(result, bool):
                        is_allowed = result
                        reason = "Allowed" if is_allowed else "Denied"
                    else:
                        is_allowed = bool(result.get("allow", False))
                        reason = "Allowed" if is_allowed else str(result.get("reason") or "Denied")
                    return is_allowed, reason

                logger.warning(
                    "OPA returned status %s. Defaulting to fail-open.",
                    response.status_code,
                )
                return True, f"fail-open (OPA status {response.status_code})"
        except Exception as e:
            logger.warning("Failed to connect to OPA: %s. Defaulting to fail-open.", e)
            return True, f"fail-open (OPA unreachable: {str(e)})"
