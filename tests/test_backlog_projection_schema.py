from pathlib import Path


def test_events_schema_defines_backlog_projection_tables():
    schema = Path("memu/events_schema.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS backlog_items" in schema
    assert "task_id         VARCHAR(128) PRIMARY KEY" in schema
    assert "idempotency_key VARCHAR(128) NOT NULL" in schema
    assert "CREATE TABLE IF NOT EXISTS backlog_events" in schema
    assert "REFERENCES backlog_items(task_id) ON DELETE CASCADE" in schema


def test_events_schema_adds_required_indexes_and_trigger():
    schema = Path("memu/events_schema.sql").read_text(encoding="utf-8")

    assert "idx_backlog_items_idempotency_key" in schema
    assert "idx_backlog_events_idempotency_key" in schema
    assert "CREATE OR REPLACE TRIGGER backlog_items_updated_at" in schema
    assert "EXECUTE FUNCTION update_updated_at();" in schema
