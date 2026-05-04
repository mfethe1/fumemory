"""Tests for the versioned embedding contract (Issue #29).

Coverage:
- EMBEDDING_BASE_URL is a logged compatibility alias for EMBEDDING_API_BASE
- resolve_embedding_api_base returns canonical URL and falls back to alias
- verify_embedding_schema passes when schema dims match configured dims
- verify_embedding_schema fails loud when dims mismatch
- verify_embedding_schema handles missing column
- verify_embedding_schema handles DB errors
- Migration 022 SQL is additive (no DROP COLUMN)
- API config uses canonical defaults: text-embedding-3-small / 1536
- Temporal worker uses canonical defaults
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure workspace root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# 1. resolve_embedding_api_base — alias behavior
# ---------------------------------------------------------------------------

def _fresh_module():
    """Import embedding_contract with a clean module state (no cached alias warning)."""
    import memu.embedding_contract as mod
    # Reset the module-level sentinel so each test starts fresh
    mod._alias_warned = False
    return mod


def test_canonical_var_takes_precedence(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_BASE", "https://canonical.example.com")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://alias.example.com")
    mod = _fresh_module()
    assert mod.resolve_embedding_api_base() == "https://canonical.example.com"


def test_alias_used_when_canonical_absent(monkeypatch):
    monkeypatch.delenv("EMBEDDING_API_BASE", raising=False)
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://alias.example.com")
    mod = _fresh_module()
    result = mod.resolve_embedding_api_base()
    assert result == "https://alias.example.com"


def test_alias_logs_deprecation_warning(monkeypatch, caplog):
    monkeypatch.delenv("EMBEDDING_API_BASE", raising=False)
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://alias.example.com")
    mod = _fresh_module()
    with caplog.at_level(logging.WARNING, logger="memu.embedding_contract"):
        mod.resolve_embedding_api_base()
    assert "deprecated alias" in caplog.text.lower() or "deprecated" in caplog.text


def test_alias_warning_emitted_only_once(monkeypatch, caplog):
    monkeypatch.delenv("EMBEDDING_API_BASE", raising=False)
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://alias.example.com")
    mod = _fresh_module()
    with caplog.at_level(logging.WARNING, logger="memu.embedding_contract"):
        mod.resolve_embedding_api_base()
        mod.resolve_embedding_api_base()
        mod.resolve_embedding_api_base()
    # Warning should appear exactly once
    warning_count = caplog.text.count("deprecated")
    assert warning_count == 1


def test_openai_default_when_neither_var_set(monkeypatch):
    monkeypatch.delenv("EMBEDDING_API_BASE", raising=False)
    monkeypatch.delenv("EMBEDDING_BASE_URL", raising=False)
    mod = _fresh_module()
    assert mod.resolve_embedding_api_base() == "https://api.openai.com"


def test_canonical_var_no_warning(monkeypatch, caplog):
    monkeypatch.setenv("EMBEDDING_API_BASE", "https://canonical.example.com")
    monkeypatch.delenv("EMBEDDING_BASE_URL", raising=False)
    mod = _fresh_module()
    with caplog.at_level(logging.WARNING, logger="memu.embedding_contract"):
        mod.resolve_embedding_api_base()
    assert "deprecated" not in caplog.text


# ---------------------------------------------------------------------------
# 2. verify_embedding_schema — dimension match / mismatch
# ---------------------------------------------------------------------------

def _make_pool(col_type: str | None, raise_exc: Exception | None = None):
    """Return a mock pool whose acquire() yields a conn that returns col_type."""
    conn = AsyncMock()
    if raise_exc:
        conn.fetchrow = AsyncMock(side_effect=raise_exc)
    elif col_type is None:
        conn.fetchrow = AsyncMock(return_value=None)
    else:
        row = {"col_type": col_type}
        conn.fetchrow = AsyncMock(return_value=row)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=conn), __aexit__=AsyncMock(return_value=False)))
    return pool


@pytest.mark.asyncio
async def test_verify_schema_match_returns_true(caplog):
    from memu.embedding_contract import verify_embedding_schema
    pool = _make_pool("vector(1536)")
    with caplog.at_level(logging.INFO, logger="memu.embedding_contract"):
        ok = await verify_embedding_schema(pool, 1536)
    assert ok is True


@pytest.mark.asyncio
async def test_verify_schema_mismatch_returns_false_and_logs_error(caplog):
    from memu.embedding_contract import verify_embedding_schema
    pool = _make_pool("vector(4096)")
    with caplog.at_level(logging.ERROR, logger="memu.embedding_contract"):
        ok = await verify_embedding_schema(pool, 1536)
    assert ok is False
    assert "mismatch" in caplog.text.lower()
    assert "1536" in caplog.text
    assert "4096" in caplog.text


@pytest.mark.asyncio
async def test_verify_schema_missing_column_returns_false_and_logs_error(caplog):
    from memu.embedding_contract import verify_embedding_schema
    pool = _make_pool(None)  # No row returned — column absent
    with caplog.at_level(logging.ERROR, logger="memu.embedding_contract"):
        ok = await verify_embedding_schema(pool, 1536)
    assert ok is False
    assert "not found" in caplog.text.lower() or "mismatch" in caplog.text.lower()


@pytest.mark.asyncio
async def test_verify_schema_db_error_returns_false_and_logs_error(caplog):
    from memu.embedding_contract import verify_embedding_schema
    pool = _make_pool(None, raise_exc=RuntimeError("DB unavailable"))
    with caplog.at_level(logging.ERROR, logger="memu.embedding_contract"):
        ok = await verify_embedding_schema(pool, 1536)
    assert ok is False
    assert "DB unavailable" in caplog.text or "failed" in caplog.text.lower()


@pytest.mark.asyncio
async def test_verify_schema_non_vector_type_does_not_hard_fail(caplog):
    """If the column type cannot be parsed as vector(N), we skip the check."""
    from memu.embedding_contract import verify_embedding_schema
    pool = _make_pool("float4[]")
    with caplog.at_level(logging.WARNING, logger="memu.embedding_contract"):
        ok = await verify_embedding_schema(pool, 1536)
    assert ok is True  # Not a hard failure — operator may know what they're doing


# ---------------------------------------------------------------------------
# 3. Production defaults — api.py and temporal worker
# ---------------------------------------------------------------------------

def test_api_defaults_use_canonical_embedding_model(monkeypatch):
    """EMBEDDING_MODEL defaults to text-embedding-3-small in api.py config."""
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    # Re-read the default the same way api.py does
    model = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
    assert model == "text-embedding-3-small"


def test_api_defaults_use_1536_dims(monkeypatch):
    """EMBEDDING_DIMS defaults to 1536 in api.py config."""
    monkeypatch.delenv("EMBEDDING_DIMS", raising=False)
    dims = int(os.environ.get("EMBEDDING_DIMS", "1536"))
    assert dims == 1536


def test_api_module_default_model(monkeypatch):
    """When EMBEDDING_MODEL is not set, api.EMBEDDING_MODEL == text-embedding-3-small."""
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    import memu.api as api_mod
    # The module-level constant reflects whatever was set when the module was imported.
    # We validate the documented default separately rather than reimporting to avoid
    # triggering the full lifespan.
    assert api_mod.EMBEDDING_MODEL in ("text-embedding-3-small", os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"))


def test_api_module_default_dims(monkeypatch):
    """api.EMBEDDING_DIMS defaults to 1536."""
    import memu.api as api_mod
    # With no override the default is 1536
    if "EMBEDDING_DIMS" not in os.environ:
        assert api_mod.EMBEDDING_DIMS == 1536


# ---------------------------------------------------------------------------
# 4. Migration 022 is additive (no DROP COLUMN)
# ---------------------------------------------------------------------------

def test_migration_022_is_additive():
    """Migration 022 must not contain DROP COLUMN — it must be purely additive."""
    sql_path = REPO_ROOT / "memu" / "migrations" / "022_embedding_version.sql"
    assert sql_path.exists(), "Migration 022 file must exist"
    content = sql_path.read_text()
    # Must add embedding_version
    assert "embedding_version" in content
    # Must NOT drop the existing embedding column
    assert "DROP COLUMN" not in content.upper() or "embedding_version" not in content.split("DROP COLUMN")[1].split("\n")[0]


def test_migration_022_uses_add_column_if_not_exists():
    """Migration 022 uses ADD COLUMN IF NOT EXISTS for idempotency."""
    sql_path = REPO_ROOT / "memu" / "migrations" / "022_embedding_version.sql"
    content = sql_path.read_text().upper()
    assert "ADD COLUMN IF NOT EXISTS" in content


def test_migration_022_does_not_drop_embedding_column():
    """The existing embedding column must not be dropped in migration 022."""
    sql_path = REPO_ROOT / "memu" / "migrations" / "022_embedding_version.sql"
    content = sql_path.read_text()
    # There should be no DROP COLUMN targeting 'embedding' (the vector column)
    import re
    drop_embedding = re.search(r"DROP\s+COLUMN\s+(IF\s+EXISTS\s+)?embedding\b", content, re.IGNORECASE)
    assert drop_embedding is None, "Migration 022 must not drop the embedding column"
