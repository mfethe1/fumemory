from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REDACT_PATTERNS = [
    re.compile(r"(memu_[A-Za-z0-9_\-]{10,})"),
    re.compile(r"(sk-[A-Za-z0-9]{20,})"),
    re.compile(r"(Bearer\s+[A-Za-z0-9\-\._~\+\/]+=*)", re.IGNORECASE),
]


@dataclass
class LogConfig:
    root: Path = Path(os.getenv("MEMU_LOG_DIR", "data/logs"))


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        out = value
        for p in REDACT_PATTERNS:
            out = p.sub("[REDACTED]", out)
        return out
    return value


def write_event(event_name: str, level: str = "info", payload: dict[str, Any] | None = None, config: LogConfig | None = None) -> dict[str, Any]:
    cfg = config or LogConfig()
    cfg.root.mkdir(parents=True, exist_ok=True)

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_name,
        "level": level,
        "payload": _redact(payload or {}),
    }
    line = json.dumps(entry, ensure_ascii=False)

    event_file = cfg.root / f"{event_name}.jsonl"
    all_file = cfg.root / "all.jsonl"

    with event_file.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    with all_file.open("a", encoding="utf-8") as f:
        f.write(line + "\n")

    return entry
