import os
import logging
import httpx
from typing import Tuple, Dict, Any

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
                    # /v1/data/memu/warden returns {"result": {"allow": true}}
                    # /v1/data/memu/warden/allow returns {"result": true}
                    is_allowed = result if isinstance(result, bool) else result.get("allow", False)
                    reason = "allowed by policy" if is_allowed else "denied by policy"
                    return is_allowed, reason
                else:
                    logger.warning(f"OPA returned status {response.status_code}. Defaulting to fail-open.")
                    return True, f"fail-open (OPA status {response.status_code})"
        except Exception as e:
            logger.warning(f"Failed to connect to OPA: {e}. Defaulting to fail-open.")
            return True, f"fail-open (OPA unreachable: {str(e)})"
