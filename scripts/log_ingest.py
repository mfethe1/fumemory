#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS structured_logs (
          id INTEGER PRIMARY KEY,
          source_file TEXT NOT NULL,
          ts TEXT,
          event TEXT,
          level TEXT,
          payload_json TEXT,
          row_hash TEXT UNIQUE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS server_raw_logs (
          id INTEGER PRIMARY KEY,
          source_file TEXT NOT NULL,
          line TEXT NOT NULL,
          row_hash TEXT UNIQUE
        )
        """
    )
    conn.commit()


def h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


def ingest_jsonl(conn: sqlite3.Connection, fpath: Path) -> int:
    n = 0
    for line in fpath.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        rh = h(f"{fpath}:{line}")
        conn.execute(
            "INSERT OR IGNORE INTO structured_logs(source_file, ts, event, level, payload_json, row_hash) VALUES (?, ?, ?, ?, ?, ?)",
            (str(fpath), row.get("ts"), row.get("event"), row.get("level"), json.dumps(row.get("payload", {})), rh),
        )
        n += 1
    return n


def ingest_raw(conn: sqlite3.Connection, fpath: Path) -> int:
    n = 0
    for line in fpath.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        rh = h(f"{fpath}:{line}")
        conn.execute(
            "INSERT OR IGNORE INTO server_raw_logs(source_file, line, row_hash) VALUES (?, ?, ?)",
            (str(fpath), line, rh),
        )
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-dir", default="data/logs")
    ap.add_argument("--db", default="data/logs/log_ingest.db")
    ap.add_argument("--raw-glob", default="")
    args = ap.parse_args()

    log_dir = Path(args.log_dir)
    db = Path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db)
    ensure_schema(conn)

    total = 0
    for f in sorted(log_dir.glob("*.jsonl")):
        total += ingest_jsonl(conn, f)

    if args.raw_glob:
        for f in Path(".").glob(args.raw_glob):
            if f.is_file():
                total += ingest_raw(conn, f)

    conn.commit()
    conn.close()
    print(json.dumps({"ok": True, "rows_seen": total, "db": str(db)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
