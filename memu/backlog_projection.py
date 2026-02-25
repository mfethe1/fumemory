from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

_TASK_RE = re.compile(r"^- \*\*Task:\*\*\s*(.+?)\s*\(OWNER:\s*(.+?)\)\s*$")
_STATUS_RE = re.compile(r"^\s*-\s*STATUS:\s*(.+?)\s*$")
_NEXT_RE = re.compile(r"^\s*-\s*NEXT ACTION:\s*(.+?)\s*$")
_BLOCKER_RE = re.compile(r"^\s*-\s*BLOCKER:\s*(.+?)\s*$")


@dataclass
class BacklogTask:
    task_id: str
    title: str
    owner: str
    lane: str
    status: str
    next_action: str | None
    blocker: str | None
    source_file: str = "BACKLOG.md"
    metadata: dict[str, Any] | None = None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:72]


def _normalize_status(status: str) -> str:
    cleaned = status.strip().lower().replace(" ", "_")
    mapping = {
        "inprogress": "in_progress",
        "in-progress": "in_progress",
        "complete": "complete",
        "done": "done",
        "active": "active",
        "blocked": "blocked",
        "pending": "pending",
        "cancelled": "cancelled",
    }
    return mapping.get(cleaned, cleaned)


def infer_lane(owner: str) -> str:
    name = owner.lower()
    if "mack" in name:
        return "infra"
    if "winnie" in name:
        return "product"
    if "rosie" in name:
        return "growth"
    if "lenny" in name:
        return "qa"
    if "team" in name:
        return "shared"
    return "unknown"


def build_task_id(title: str, owner: str) -> str:
    digest = hashlib.sha1(f"{title}|{owner}".encode()).hexdigest()[:10]
    return f"{_slug(title)}-{digest}"


def parse_backlog_markdown(markdown: str, *, source_file: str = "BACKLOG.md") -> list[BacklogTask]:
    lines = markdown.splitlines()
    tasks: list[BacklogTask] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        m = _TASK_RE.match(line)
        if not m:
            i += 1
            continue

        title = m.group(1).strip()
        owner = m.group(2).strip()
        status = "pending"
        next_action = None
        blocker = None

        j = i + 1
        while j < len(lines):
            if _TASK_RE.match(lines[j]) or lines[j].startswith("## ") or lines[j].startswith("### "):
                break
            sm = _STATUS_RE.match(lines[j])
            if sm:
                status = _normalize_status(sm.group(1))
            nm = _NEXT_RE.match(lines[j])
            if nm:
                next_action = nm.group(1).strip()
            bm = _BLOCKER_RE.match(lines[j])
            if bm:
                blocker = bm.group(1).strip()
            j += 1

        tasks.append(
            BacklogTask(
                task_id=build_task_id(title, owner),
                title=title,
                owner=owner,
                lane=infer_lane(owner),
                status=_normalize_status(status),
                next_action=next_action,
                blocker=blocker,
                source_file=source_file,
                metadata={"title": title},
            )
        )
        i = j

    return tasks
