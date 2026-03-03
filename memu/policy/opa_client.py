import os
import logging
import httpx
from typing import Dict, Any, Tuple

logger = logging.getLogger(__name__)

class OPAClient:
    """Client for Open Policy Agent."""

    @classmethod
    async def evaluate(cls, policy_path: str, input_data: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Evaluate a policy in OPA.
        Returns (is_allowed, reason).
        """
        opa_url = os.getenv("OPA_URL", "http://localhost:8181").rstrip("/")
        url = f"{opa_url}/v1/data/{policy_path}"
        
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.post(url, json={"input": input_data})
                
                if response.status_code != 200:
                    logger.warning(f"OPA error {response.status_code} for {policy_path}: {response.text}")
                    return (True, f"OPA returned {response.status_code}, fail-open allowed")
                
                data = response.json()
                
                # OPA response format: {"result": {...}} or {"result": true/false}
                if "result" not in data:
                    logger.warning(f"OPA invalid response format for {policy_path}")
                    return (True, "OPA invalid format, fail-open allowed")
                
                result = data["result"]
                
                # Simple boolean result
                if isinstance(result, bool):
                    return (result, "Allowed" if result else "Denied by policy")
                
                # Complex result map (e.g. {"allow": true, "reason": "ok"})
                if isinstance(result, dict):
                    allow = result.get("allow", False)
                    reason = result.get("reason", "Denied by policy") if not allow else "Allowed"
                    return (allow, reason)
                
                logger.warning(f"OPA unexpected result type {type(result)} for {policy_path}")
                return (True, "OPA unexpected format, fail-open allowed")

        except Exception as e:
            logger.warning(f"OPA unreachable or failed: {str(e)}. Falling back to allow.")
            return (True, f"OPA error ({str(e)}), fail-open allowed")
