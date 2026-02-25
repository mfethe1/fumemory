#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path


def parse_ts(s: str | None):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-dir", default="data/logs")
    ap.add_argument("--event", default="")
    ap.add_argument("--level", default="")
    ap.add_argument("--contains", default="")
    ap.add_argument("--from-ts", default="")
    ap.add_argument("--to-ts", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from_ts = parse_ts(args.from_ts) if args.from_ts else None
    to_ts = parse_ts(args.to_ts) if args.to_ts else None

    source = Path(args.log_dir) / (f"{args.event}.jsonl" if args.event else "all.jsonl")
    if not source.exists():
        print("[]" if args.json else f"No log file: {source}")
        return 0

    out = []
    with source.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if args.level and row.get("level") != args.level:
                continue
            txt = json.dumps(row, ensure_ascii=False)
            if args.contains and args.contains.lower() not in txt.lower():
                continue
            ts = parse_ts(row.get("ts"))
            if from_ts and ts and ts < from_ts:
                continue
            if to_ts and ts and ts > to_ts:
                continue
            out.append(row)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for r in out:
            print(f"{r.get('ts')} [{r.get('level')}] {r.get('event')} {json.dumps(r.get('payload', {}), ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
