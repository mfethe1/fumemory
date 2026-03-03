"""Cryptographic signing for the Swarm Event Mesh.

Ed25519 asymmetric signing ensures every event on the mesh is:
1. Authenticated — we know which gateway produced it
2. Non-repudiable — the gateway can't deny producing it
3. Tamper-evident — any modification invalidates the signature

Key lifecycle:
- Ephemeral keypair generated per gateway boot (boot.py)
- Public key registered in gateway_registry
- Private key held in memory only (never persisted to disk)
- On shutdown, key is marked revoked in registry

Uses PyNaCl (libsodium bindings) for Ed25519 — faster and simpler than
cryptography.hazmat for pure signing operations. Falls back to
cryptography if nacl is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# Try PyNaCl first (faster, simpler), fall back to cryptography
_BACKEND: str = "none"

try:
    from nacl.signing import SigningKey, VerifyKey
    from nacl.exceptions import BadSignatureError
    _BACKEND = "nacl"
except ImportError:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
        from cryptography.hazmat.primitives import serialization
        from cryptography.exceptions import InvalidSignature
        _BACKEND = "cryptography"
    except ImportError:
        logger.warning(
            "No crypto backend available. Install PyNaCl or cryptography. "
            "Event signing will be disabled."
        )


class GatewayKeyPair:
    """Ed25519 keypair for a single gateway instance.
    
    Generated once at boot, used to sign all events this gateway produces.
    The public key is registered in gateway_registry for verification.
    """

    def __init__(self):
        if _BACKEND == "nacl":
            self._signing_key = SigningKey.generate()
            self._verify_key = self._signing_key.verify_key
            self.public_key_bytes: bytes = bytes(self._verify_key)
            self.public_key_hex: str = self.public_key_bytes.hex()
        elif _BACKEND == "cryptography":
            self._private_key = Ed25519PrivateKey.generate()
            self._public_key = self._private_key.public_key()
            self.public_key_bytes = self._public_key.public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
            self.public_key_hex = self.public_key_bytes.hex()
        else:
            self.public_key_bytes = b""
            self.public_key_hex = ""
            logger.warning("No crypto backend — signing disabled")

    def sign(self, payload: bytes) -> str:
        """Sign payload, return hex-encoded signature."""
        if _BACKEND == "nacl":
            signed = self._signing_key.sign(payload)
            return signed.signature.hex()
        elif _BACKEND == "cryptography":
            sig = self._private_key.sign(payload)
            return sig.hex()
        else:
            return ""

    @staticmethod
    def verify(public_key_hex: str, payload: bytes, signature_hex: str) -> bool:
        """Verify a signature against a public key."""
        if not signature_hex or not public_key_hex:
            return False
        try:
            pub_bytes = bytes.fromhex(public_key_hex)
            sig_bytes = bytes.fromhex(signature_hex)

            if _BACKEND == "nacl":
                vk = VerifyKey(pub_bytes)
                vk.verify(payload, sig_bytes)
                return True
            elif _BACKEND == "cryptography":
                pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
                pub_key.verify(sig_bytes, payload)
                return True
        except Exception as e:
            logger.debug(f"Signature verification failed: {e}")
            return False

        return False


def canonical_event_hash(
    task_id: str | UUID,
    timestamp: str | datetime,
    event_type: str,
    payload: dict[str, Any],
) -> bytes:
    """Create a deterministic hash of an event for signing.
    
    The canonical form ensures the same event always produces
    the same hash regardless of dict ordering or whitespace.
    """
    if isinstance(timestamp, datetime):
        ts_str = timestamp.isoformat()
    else:
        ts_str = str(timestamp)

    canonical = json.dumps(
        {
            "task_id": str(task_id),
            "timestamp": ts_str,
            "event_type": str(event_type),
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def get_backend() -> str:
    """Return the active crypto backend name."""
    return _BACKEND
