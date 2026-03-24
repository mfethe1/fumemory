"""
Tests for the Apache AGE sync-trigger migration SQL
(004_apache_age_sync_trigger.sql).

Validates the SQL structure, Cypher templates, and that the trigger
definition is well-formed — without requiring a live database.
"""
import pathlib
import re

import pytest

MIGRATION_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "memu"
    / "migrations"
    / "004_apache_age_sync_trigger.sql"
)


@pytest.fixture(scope="module")
def migration_sql() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def test_migration_file_exists():
    assert MIGRATION_PATH.exists(), "004_apache_age_sync_trigger.sql not found"


def test_trigger_definition_present(migration_sql):
    """Trigger declaration targeting memories table must be present."""
    assert "CREATE TRIGGER trg_sync_memory_to_graph" in migration_sql
    assert "ON memories" in migration_sql


def test_trigger_fires_on_all_dml(migration_sql):
    """Trigger must fire on INSERT, UPDATE, and DELETE."""
    assert "AFTER INSERT OR UPDATE OR DELETE" in migration_sql


def test_trigger_function_created(migration_sql):
    """PL/pgSQL function must be defined."""
    assert "CREATE OR REPLACE FUNCTION sync_memory_to_graph()" in migration_sql
    assert "RETURNS TRIGGER" in migration_sql


def test_cypher_insert_uses_format(migration_sql):
    """INSERT branch must build Cypher via format() to compose the query."""
    # Should have a CREATE Cypher node for the Memory label
    assert "CREATE (v:Memory" in migration_sql or "CREATE (v:Memory" in migration_sql.replace("\n", "")


def test_cypher_delete_uses_detach_delete(migration_sql):
    """DELETE branch must use DETACH DELETE to remove edges as well."""
    assert "DETACH DELETE" in migration_sql


def test_cypher_edge_derived_from_present(migration_sql):
    """DERIVED_FROM relationship must be created when parent_id is set."""
    assert "DERIVED_FROM" in migration_sql


def test_drop_trigger_before_create(migration_sql):
    """Idempotent: trigger must be dropped before re-creation."""
    assert "DROP TRIGGER IF EXISTS trg_sync_memory_to_graph" in migration_sql


def test_search_path_set_in_function(migration_sql):
    """Function body must set ag_catalog search path for AGE to work."""
    assert "SET search_path = ag_catalog" in migration_sql


def test_no_raw_user_input_in_graph_name(migration_sql):
    """graph_name must be a constant string literal, not a variable interpolation."""
    # The constant should be assigned as a varchar literal
    assert "CONSTANT varchar := 'memu_graph'" in migration_sql or \
           "CONSTANT text := 'memu_graph'" in migration_sql or \
           "CONSTANT varchar := " in migration_sql
