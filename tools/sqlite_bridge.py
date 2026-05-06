"""Bridge per-agent SQLite memory stores to memu local instance.

Scans ~/.openclaw/memory/*.sqlite, converts each record to a memu MemoryRecord,
and POSTs to localhost:8000/memories with agent_id set to the source agent name.

Deduplication: idempotency_key=sqlite:<agent>:<rowid> on evidence-kind writes
ensures repeated runs are safe — same key + same content returns the existing
record rather than creating a duplicate.

State tracking: ~/.openclaw/memory/.memu_bridge_state.json persists the last
imported rowid per SQLite file so only new rows are forwarded on each run.

Usage:
  python tools/sqlite_bridge.py --once              # backfill and exit (default)
  python tools/sqlite_bridge.py --watch             # backfill then poll every N seconds
  python tools/sqlite_bridge.py --watch --interval 60
  python tools/sqlite_bridge.py --dir /custom/path --url http://memu:8000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("sqlite_bridge")

# ---------------------------------------------------------------------------
# Configuration (overridable via environment variables)
# ---------------------------------------------------------------------------

OPENCLAW_MEMORY_DIR = Path(
    os.environ.get("OPENCLAW_MEMORY_DIR", "~/.openclaw/memory")
).expanduser()

MEMU_API_URL = (
    os.environ.get("MEMU_API_URL") or os.environ.get("MEMU_BASE_URL") or "http://localhost:8000"
).rstrip("/")

MEMU_API_KEY = os.environ.get("MEMU_API_KEY", "memu-dev-key")

STATE_FILE = Path(
    os.environ.get("SQLITE_BRIDGE_STATE", "~/.openclaw/memory/.memu_bridge_state.json")
).expanduser()

POLL_INTERVAL_DEFAULT = 30

# ---------------------------------------------------------------------------
# Schema column detection
# ---------------------------------------------------------------------------

_CONTENT_CANDIDATES = ("value", "content", "text", "body", "fact", "note", "message")
_TYPE_CANDIDATES = ("type", "memory_type", "kind", "category")
_TIMESTAMP_CANDIDATES = ("created_at", "timestamp", "ts", "created", "date", "time")

_MEMORY_TYPE_MAP: dict[str, str] = {
    "fact": "fact",
    "lesson": "lesson",
    "decision": "decision",
    "pattern": "pattern",
    "failure": "failure",
    "observation": "observation",
    "reflection": "reflection",
    "plan": "plan",
    "goal": "goal",
    "note": "observation",
    "info": "fact",
    "knowledge": "lesson",
}


def _pick_column(candidates: tuple[str, ...], columns: list[str]) -> str | None:
    """Return the first candidate that exists in columns (case-insensitive)."""
    lower_map = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None


def map_memory_type(raw: Any) -> str:
    """Map a raw SQLite type value to a memu MemoryType string."""
    if not raw:
        return "fact"
    return _MEMORY_TYPE_MAP.get(str(raw).lower().strip(), "fact")


def row_to_payload(row: dict[str, Any], agent_name: str, rowid: int) -> dict[str, Any]:
    """Convert a SQLite row dict to a memu POST /memories payload.

    Uses idempotency_key=sqlite:<agent>:<rowid> so repeated imports are safe.
    Imports as evidence kind to leverage idempotency_key deduplication.
    """
    columns = list(row.keys())

    content_col = _pick_column(_CONTENT_CANDIDATES, columns)
    type_col = _pick_column(_TYPE_CANDIDATES, columns)
    ts_col = _pick_column(_TIMESTAMP_CANDIDATES, columns)

    content = row.get(content_col) if content_col else None
    if not content:
        # Fallback: serialise all non-None fields into a readable string
        parts = [f"{k}: {v}" for k, v in row.items() if v is not None]
        content = " | ".join(parts) if parts else repr(row)

    memory_type = map_memory_type(row.get(type_col) if type_col else None)

    metadata: dict[str, Any] = {
        "sqlite_source": agent_name,
        "sqlite_rowid": rowid,
        "bridge": "sqlite_agent_bridge",
    }
    if ts_col and row.get(ts_col) is not None:
        metadata["source_timestamp"] = str(row[ts_col])

    return {
        "content": str(content),
        "memory_type": memory_type,
        "memory_kind": "evidence",
        "agent_id": agent_name,
        "metadata": metadata,
        "confidence": 1.0,
        "idempotency_key": f"sqlite:{agent_name}:{rowid}",
    }


# ---------------------------------------------------------------------------
# SQLite reading
# ---------------------------------------------------------------------------

_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(name: str) -> str:
    """Reject table names that could be used for SQL injection."""
    if not _SAFE_IDENTIFIER_RE.match(name):
        raise ValueError(f"Unsafe SQLite identifier: {name!r}")
    return name


def read_rows_after(
    db_path: Path, last_rowid: int
) -> tuple[list[tuple[dict[str, Any], int]], int]:
    """Read rows with rowid > last_rowid from the first user table.

    Opens the database read-only to avoid accidental writes.
    Returns (list of (row_dict, rowid), new_max_rowid).
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        if not tables:
            return [], last_rowid

        table = _validate_identifier(tables[0])
        raw_rows = conn.execute(
            f"SELECT rowid, * FROM {table} WHERE rowid > ? ORDER BY rowid ASC",  # noqa: S608
            (last_rowid,),
        ).fetchall()

        result: list[tuple[dict[str, Any], int]] = []
        new_max = last_rowid
        for raw in raw_rows:
            d = dict(raw)
            rid = int(d.pop("rowid"))
            result.append((d, rid))
            if rid > new_max:
                new_max = rid
        return result, new_max
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def load_state(state_file: Path) -> dict[str, int]:
    """Load per-file last_rowid tracking state."""
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except Exception as exc:
            logger.warning("Failed to load bridge state from %s: %s", state_file, exc)
    return {}


def save_state(state_file: Path, state: dict[str, int]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# memu API client
# ---------------------------------------------------------------------------


async def post_memory(
    client: httpx.AsyncClient,
    payload: dict[str, Any],
    memu_url: str = MEMU_API_URL,
    api_key: str = MEMU_API_KEY,
) -> dict[str, Any] | None:
    """POST a memory payload to memu. Returns response JSON or None on error.

    HTTP 200/201: memory created or exact-replay idempotent return.
    HTTP 409: idempotency conflict (same key, different content) — logged.
    Other errors: logged and None returned so the bridge continues.
    """
    try:
        resp = await client.post(
            f"{memu_url}/memories",
            json=payload,
            headers={"X-MemU-Key": api_key},
            timeout=15.0,
        )
        if resp.status_code in (200, 201):
            return resp.json()
        if resp.status_code == 409:
            logger.warning(
                "Idempotency conflict for key %r: %s",
                payload.get("idempotency_key"),
                resp.text[:200],
            )
            return None
        logger.error("memu API error %d for key %r: %s",
                     resp.status_code, payload.get("idempotency_key"), resp.text[:200])
        return None
    except Exception as exc:
        logger.error("Failed to POST memory (key=%r) to memu: %s",
                     payload.get("idempotency_key"), exc)
        return None


# ---------------------------------------------------------------------------
# Bridge logic
# ---------------------------------------------------------------------------


async def sync_file(
    db_path: Path,
    state: dict[str, int],
    client: httpx.AsyncClient,
    memu_url: str = MEMU_API_URL,
    api_key: str = MEMU_API_KEY,
) -> int:
    """Sync new rows from one SQLite file to memu.

    Returns the count of rows successfully imported.
    Updates state[str(db_path)] with the new max rowid.
    """
    agent_name = db_path.stem  # e.g. "mack" from "mack.sqlite"
    state_key = str(db_path)
    last_rowid = state.get(state_key, 0)

    try:
        rows, new_max = read_rows_after(db_path, last_rowid)
    except Exception as exc:
        logger.error("Failed to read %s: %s", db_path, exc)
        return 0

    if not rows:
        logger.debug("No new rows in %s (last_rowid=%d)", db_path.name, last_rowid)
        return 0

    imported = 0
    for row_dict, rowid in rows:
        payload = row_to_payload(row_dict, agent_name, rowid)
        result = await post_memory(client, payload, memu_url, api_key)
        if result is not None:
            imported += 1

    state[state_key] = new_max
    logger.info(
        "Synced %s (%s): %d/%d rows imported, last_rowid=%d",
        db_path.name,
        agent_name,
        imported,
        len(rows),
        new_max,
    )
    return imported


async def sync_all(
    memory_dir: Path,
    state: dict[str, int],
    state_file: Path,
    client: httpx.AsyncClient,
    memu_url: str = MEMU_API_URL,
    api_key: str = MEMU_API_KEY,
) -> int:
    """Sync all *.sqlite files in memory_dir to memu.

    Returns the total count of rows imported across all files.
    Saves state after every full sweep.
    """
    db_files = sorted(f for f in memory_dir.glob("*.sqlite") if not f.name.startswith("."))

    if not db_files:
        logger.info("No *.sqlite files found in %s", memory_dir)
        return 0

    total = 0
    for db_path in db_files:
        total += await sync_file(db_path, state, client, memu_url, api_key)

    save_state(state_file, state)
    return total


async def run(
    memory_dir: Path = OPENCLAW_MEMORY_DIR,
    memu_url: str = MEMU_API_URL,
    api_key: str = MEMU_API_KEY,
    state_file: Path = STATE_FILE,
    once: bool = True,
    poll_interval: int = POLL_INTERVAL_DEFAULT,
) -> None:
    """Run the bridge: one-shot backfill or continuous watch loop."""
    state = load_state(state_file)

    async with httpx.AsyncClient() as client:
        if once:
            await sync_all(memory_dir, state, state_file, client, memu_url, api_key)
        else:
            while True:
                await sync_all(memory_dir, state, state_file, client, memu_url, api_key)
                logger.info("Sleeping %ds before next poll", poll_interval)
                await asyncio.sleep(poll_interval)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bridge OpenClaw per-agent SQLite memories to memu"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        default=True,
        help="Backfill once and exit (default)",
    )
    mode.add_argument(
        "--watch",
        action="store_true",
        help="Backfill then poll for new rows continuously",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=POLL_INTERVAL_DEFAULT,
        metavar="SECONDS",
        help=f"Poll interval in seconds when --watch is set (default: {POLL_INTERVAL_DEFAULT})",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=OPENCLAW_MEMORY_DIR,
        metavar="PATH",
        help="Directory containing per-agent *.sqlite files",
    )
    parser.add_argument(
        "--url",
        default=MEMU_API_URL,
        metavar="URL",
        help="memu API base URL",
    )
    parser.add_argument(
        "--key",
        default=MEMU_API_KEY,
        metavar="KEY",
        help="memu API key",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=STATE_FILE,
        metavar="PATH",
        help="JSON file for tracking last synced rowid per agent",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    asyncio.run(
        run(
            memory_dir=args.dir,
            memu_url=args.url,
            api_key=args.key,
            state_file=args.state,
            once=not args.watch,
            poll_interval=args.interval,
        )
    )


if __name__ == "__main__":
    main()
