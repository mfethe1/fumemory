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


def _clean_endpoint(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned or cleaned.lower() in {"none", "null", "undefined"}:
        return None
    return cleaned
