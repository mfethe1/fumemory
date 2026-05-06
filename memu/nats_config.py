from __future__ import annotations

import os


def resolve_nats_endpoints() -> tuple[str | None, str | None]:
    """Resolve local + fallback NATS endpoints from the current env.

    Contract order:
    1. NATS_LOCAL_URL / NATS_RAILWAY_URL (preferred explicit dual-endpoint config)
    2. NATS_URL as a backwards-compatible single-endpoint fallback

    Returns:
        tuple[local_url, railway_url]
    """
    local = _clean_endpoint(os.environ.get("NATS_LOCAL_URL"))
    railway = _clean_endpoint(os.environ.get("NATS_RAILWAY_URL"))

    legacy = _clean_endpoint(os.environ.get("NATS_URL"))
    if not local and legacy:
        local = legacy

    return local, railway


def primary_nats_url(default: str = "nats://localhost:4222") -> str:
    """Return the best available primary NATS URL for single-endpoint callers."""
    local, railway = resolve_nats_endpoints()
    return local or railway or default


def resolve_local_nkey_seed() -> str | None:
    """Return the local NATS NKey seed from the environment, if set.

    NATS_LOCAL_NKEY_SEED is the Ed25519 user seed (starts with 'SU') for the
    local NATS instance only.  It is intentionally separate from NATS_NKEY_SEED
    (used for Synadia Cloud / NGS / Railway) so that local and Railway nodes
    can carry different credentials.

    Generate with: infra/local-nats/gen-nkeys.sh
    """
    return _clean_endpoint(os.environ.get("NATS_LOCAL_NKEY_SEED"))


def _clean_endpoint(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned or cleaned.lower() in {"none", "null", "undefined"}:
        return None
    return cleaned
