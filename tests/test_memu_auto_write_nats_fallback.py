import asyncio
import importlib.util
import json
import pathlib
import sys


def _load_module():
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "memu_auto_write.py"
    spec = importlib.util.spec_from_file_location("memu_auto_write", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_emit_nats_event_writes_outbox_when_publish_fails(monkeypatch, tmp_path):
    mod = _load_module()

    monkeypatch.setenv("NATS_OUTBOX_DIR", str(tmp_path))
    monkeypatch.setitem(sys.modules, "nats", object())

    asyncio.run(mod.emit_nats_event("memory_written", "task-1", {"id": "123"}, "lenny"))

    outbox_files = list(tmp_path.glob("lenny-*.json"))
    assert outbox_files, "expected outbox payload file"

    payload = json.loads(outbox_files[0].read_text(encoding="utf-8"))
    assert payload["subject"] == "swarm.events"
    assert payload["event"]["event_type"] == "memory_written"
    assert "publish_command" in payload
