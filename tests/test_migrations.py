from __future__ import annotations

from pathlib import Path

import pytest

from memu import migrations


class _AcquireContext:
    def __init__(self, conn: _FakeConnection):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, conn: _FakeConnection):
        self._conn = conn

    def acquire(self):
        return _AcquireContext(self._conn)


class _FakeConnection:
    def __init__(self):
        self.records = {"001_already_successful.sql": True}
        self.applied_sql: list[str] = []
        self.deleted_versions: list[str] = []

    async def fetchrow(self, query: str, version: str):
        if "schema_migrations" not in query:
            raise AssertionError(f"unexpected fetchrow query: {query}")
        if version not in self.records:
            return None
        return {"success": self.records[version]}

    async def execute(self, query: str, *args):
        normalized = " ".join(query.split())

        if normalized.startswith("CREATE EXTENSION"):
            raise RuntimeError("extension unavailable")

        if normalized.startswith("CREATE TABLE") or normalized.startswith("ALTER TABLE"):
            return "OK"

        if normalized.startswith("DELETE FROM schema_migrations"):
            version = args[0]
            self.records.pop(version, None)
            self.deleted_versions.append(version)
            return "DELETE 1"

        if normalized.startswith("INSERT INTO schema_migrations"):
            version = args[0]
            self.records[version] = "TRUE" in normalized and "FALSE" not in normalized
            return "INSERT 0 1"

        if "BROKEN SQL" in query:
            raise RuntimeError("syntax error at or near BROKEN")

        self.applied_sql.append(query.strip())
        return "OK"


@pytest.mark.asyncio
async def test_failed_migration_is_recorded_then_raised_and_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    package_dir = tmp_path / "memu"
    migration_dir = package_dir / "migrations"
    migration_dir.mkdir(parents=True)
    monkeypatch.setattr(migrations, "__file__", str(package_dir / "migrations.py"))

    (migration_dir / "001_already_successful.sql").write_text("SELECT 'skip';")
    (migration_dir / "002_success.sql").write_text("SELECT 'apply once';")
    failed_migration = migration_dir / "003_fails.sql"
    failed_migration.write_text("BROKEN SQL;")

    conn = _FakeConnection()
    pool = _FakePool(conn)

    with pytest.raises(RuntimeError, match="syntax error at or near BROKEN"):
        await migrations.run_migrations(pool)

    assert "SELECT 'skip';" not in conn.applied_sql
    assert conn.records["001_already_successful.sql"] is True
    assert conn.records["002_success.sql"] is True
    assert conn.records["003_fails.sql"] is False

    failed_migration.write_text("SELECT 'fixed';")

    await migrations.run_migrations(pool)

    assert conn.deleted_versions == ["003_fails.sql"]
    assert conn.records["003_fails.sql"] is True
    assert conn.applied_sql.count("SELECT 'apply once';") == 1
    assert "SELECT 'fixed';" in conn.applied_sql
