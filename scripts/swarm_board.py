#!/usr/bin/env python3
"""Minimal local swarm board generator for fumemory."""
from __future__ import annotations

import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASK_DIR = ROOT / "docs" / "swarm_tasks"
OUT = ROOT / "ui" / "swarm-board.html"


def load_tasks():
    tasks = []
    if TASK_DIR.exists():
        for path in sorted(TASK_DIR.glob("*.json")):
            tasks.append(json.loads(path.read_text(encoding="utf-8")))
    return tasks


def render(tasks):
    rows = []
    for t in tasks:
        rows.append(
            f"<tr><td>{html.escape(t['task_id'])}</td><td>{html.escape(t['project'])}</td><td>{html.escape(t['lane'])}</td><td>{html.escape(t['status'])}</td><td>{html.escape(t['priority'])}</td><td>{html.escape(str(t.get('assigned_gateway') or '-'))}</td><td>{html.escape(str(t.get('review_status') or '-'))}</td></tr>"
        )
    return f"<!doctype html><html><head><meta charset='utf-8'><title>Swarm Board</title><style>body{{font-family:Segoe UI,Arial,sans-serif;background:#0d1321;color:#eef2ff;margin:0;padding:24px}}.wrap{{max-width:1100px;margin:0 auto}}.card{{background:#151d33;border:1px solid #293455;border-radius:14px;padding:18px;margin:14px 0}}table{{width:100%;border-collapse:collapse}}td,th{{border-bottom:1px solid #2e3a60;padding:10px;text-align:left}}th{{color:#9fb0d9}}code{{background:#0f1730;padding:2px 6px;border-radius:6px}}</style></head><body><div class='wrap'><div class='card'><h1>Swarm Board</h1><p>Minimal control-plane board for gateway/task review.</p></div><div class='card'><h2>Tasks</h2><table><thead><tr><th>Task ID</th><th>Project</th><th>Lane</th><th>Status</th><th>Priority</th><th>Gateway</th><th>Review</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></div></body></html>"


def main():
    tasks = load_tasks()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(tasks), encoding="utf-8")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
