#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import shutil
import sqlite3
from pathlib import Path


def rotate_jsonl(log_dir: Path, max_mb: int, keep: int) -> list[str]:
    rotated = []
    max_bytes = max_mb * 1024 * 1024
    for f in sorted(log_dir.glob("*.jsonl")):
        if f.stat().st_size <= max_bytes:
            continue
        ts = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        out = f.with_name(f"{f.stem}.{ts}.jsonl.gz")
        with f.open("rb") as src, gzip.open(out, "wb") as dst:
            shutil.copyfileobj(src, dst)
        f.write_text("", encoding="utf-8")
        rotated.append(str(out))

    for stem in {p.stem.split('.')[0] for p in log_dir.glob('*.jsonl.gz')}:
        files = sorted(log_dir.glob(f"{stem}.*.jsonl.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            old.unlink(missing_ok=True)
    return rotated


def archive_db_monthly(db_path: Path, archive_dir: Path) -> str | None:
    if not db_path.exists():
        return None
    month = dt.datetime.utcnow().strftime("%Y-%m")
    archive_dir.mkdir(parents=True, exist_ok=True)
    out = archive_dir / f"logs-{month}.sqlite"
    shutil.copy2(db_path, out)
    return str(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-dir", default="data/logs")
    ap.add_argument("--max-mb", type=int, default=50)
    ap.add_argument("--keep", type=int, default=3)
    ap.add_argument("--db", default="data/logs/log_ingest.db")
    ap.add_argument("--archive-dir", default="data/logs/archive")
    args = ap.parse_args()

    log_dir = Path(args.log_dir)
    rotated = rotate_jsonl(log_dir, args.max_mb, args.keep)
    archived = archive_db_monthly(Path(args.db), Path(args.archive_dir))
    print({"ok": True, "rotated": rotated, "archived_db": archived})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
