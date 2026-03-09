#!/usr/bin/env python3
"""Unified heartbeat scanner for file-level TODO/FIXME discovery.

This script is designed for low-context RLM execution:
1) Scan configured repos for markers (TODO/FIXME/HACK/XXX)
2) Normalize + deduplicate tasks using source_fingerprint/source_ref
3) Compute risk score and required reviewability fields
4) Insert into memU backlog (Postgres)
5) Publish structured NATS events so the swarm can pick up claims
6) Optionally create GitHub issues for scan-originated tasks
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import asyncpg

from memu.nats_publisher import NATSEventPublisher
from memu.cluster import NATSClusterManager
from memu.tenancy import DEFAULT_TENANT_ID


@dataclass
class ScanHit:
    path: str
    line_no: int
    marker: str
    text: str


def get_repo_roots(raw: str | None) -> list[str]:
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()]

    env_paths = os.environ.get("TASK_REGISTRY_REPOS", "")
    if env_paths:
        return [p.strip() for p in env_paths.split(",") if p.strip()]

    return []


def build_scan_command(repo: str, include_ext: str, exclude_globs: list[str]) -> list[str]:
    pattern = os.environ.get("TASK_MARKER_PATTERN", r"TODO|FIXME|HACK|XXX|BLOCKED")
    cmd = ["rg", "-n", "-S", pattern, repo]

    for glob in include_ext.split(","):
        ext = glob.strip()
        if not ext:
            continue
        cmd.extend(["--glob", f"*{ext}"])

    for ex in exclude_globs:
        if ex:
            cmd.extend(["-g", f"!{ex}"])

    # Exclude known non-source noise
    cmd.extend([
        "-g", "!.git",
        "-g", "!.venv",
        "-g", "!node_modules",
        "-g", "!.next",
        "-g", "!dist",
        "-g", "!build",
        "-g", "!target",
    ])
    return cmd


def parse_hits(stdout: str) -> list[ScanHit]:
    hits: list[ScanHit] = []
    for raw_line in stdout.splitlines():
        # Expected: /path/to/file:123:content
        parts = raw_line.split(":", 2)
        if len(parts) < 3:
            continue
        path, line_no, text = parts
        try:
            ln = int(line_no)
        except ValueError:
            continue

        marker_match = re.search(r"\b(TODO|FIXME|HACK|XXX|BLOCKED)\b", text)
        marker = marker_match.group(1) if marker_match else "MARKER"
        text = text.strip()
        if not text:
            continue

        hits.append(ScanHit(path=path.strip(), line_no=ln, marker=marker, text=text))
    return hits


def compute_risk(path: str, text: str, marker: str) -> int:
    marker_weights = {
        "TODO": 20,
        "FIXME": 35,
        "HACK": 50,
        "XXX": 45,
        "BLOCKED": 30,
    }
    base = marker_weights.get(marker, 20)

    score = base
    lowered = text.lower()

    high_risk_keywords = [
        "security",
        "auth",
        "payment",
        "credential",
        "secret",
        "token",
        "database",
        "migration",
        "money",
        "revenue",
        "outage",
        "downtime",
        "production",
        "critical",
        "panic",
    ]
    score += 15 * sum(1 for k in high_risk_keywords if k in lowered)

    danger_path = [
        "auth",
        "security",
        "payment",
        "finance",
        "infra",
        "network",
        "deploy",
        "ci",
    ]
    score += 8 * sum(1 for k in danger_path if f"/{k}/" in path.lower())

    return max(1, min(100, score))


def make_completion_criteria(hit: ScanHit) -> str:
    return (
        f"Refine and implement the issue at {hit.path}:{hit.line_no}. "
        f"Outcome must be objectively verifiable (diff in file, tests updated, or explicit command output)."
    )


def make_task_payload(hit: ScanHit, owner: str | None, menu_bucket: str | None) -> dict[str, Any]:
    project = os.path.basename(os.path.dirname(hit.path)) or "global"
    source_ref = f"{hit.path}:{hit.line_no}:{hit.marker}"
    content = (
        f"[{hit.marker}] {hit.text} "
        f"(source={source_ref})"
    )
    risk = compute_risk(hit.path, hit.text, hit.marker)

    fingerprint_seed = f"{source_ref}|{hit.text[:140]}|{project}|{owner or ''}"
    source_fingerprint = hashlib.sha256(fingerprint_seed.encode()).hexdigest()[:64]

    return {
        "task": content[:500],
        "owner_id": owner,
        "lane": project,
        "priority": "P2" if risk < 40 else ("P1" if risk < 70 else "P0"),
        "risk_score": risk,
        "source": "scan",
        "source_ref": source_ref,
        "project": project,
        "menu_bucket": menu_bucket,
        "completion_criteria": make_completion_criteria(hit),
        "source_fingerprint": source_fingerprint,
        "metadata": {
            "detected_line": hit.text,
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "detector": "task_registry_scanner",
            "path": hit.path,
            "line_no": hit.line_no,
            "marker": hit.marker,
        },
    }


def determine_status(risk_score: int) -> str:
    # Invert confidence model:
    # low risk -> proceed; high risk -> ask before proceeding
    return "pending" if risk_score <= 80 else "blocked"

def build_refine_status(risk_score: int) -> str:
    return "needs_revision" if risk_score > 80 else "pending"

async def ensure_task_table(conn):
    # Lightweight check so local dev can fail fast with actionable error
    table = await conn.fetchval("""
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'backlog'
        )
    """)
    if not table:
        raise RuntimeError("backlog table missing. Ensure memu migrations are applied")


def refine_candidate(payload: dict[str, Any]) -> tuple[bool, str]:
    task_text = payload["task"].lower()
    marker = payload["metadata"]["marker"]

    action_verbs = [
        "fix", "implement", "add", "create", "remove", "update", "refactor",
        "patch", "investigate", "adjust", "document", "test", "deploy",
    ]

    measurable_patterns = [
        r"\d+\s*%?\b",  # numeric metric
        r"\b(file|tests|api|endpoint|route|migration|schema|config)\b",
        r"/tmp/|/var/|\.py|\.ts|\.js",
    ]

    has_action = any(v in task_text for v in action_verbs)
    has_source = "source_ref" in payload and bool(payload["source_ref"])
    has_measurable = any(re.search(p, task_text) for p in measurable_patterns)

    if marker in {"HACK", "XXX"} and not has_action:
        return False, "Action verb missing for high-risk marker"

    if not has_source:
        return False, "Task missing source reference"

    if not has_measurable:
        return False, "Outcome criteria lacks measurable artifact or file reference"

    if len(task_text) < 40:
        return False, "Task text too short to execute reliably"

    return has_action, "pass" if has_action else "refinement check failed"


async def publish_events(publisher: NATSEventPublisher | None, payload: dict[str, Any], risk: int) -> None:
    if not publisher:
        return

    await publisher.publish_task_drafted(
        agent_id=payload.get("owner_id") or "system",
        task_id=payload["task_id"],
        title=payload["task"],
        source=payload["source"],
        risk_score=risk,
        project=payload["project"],
    )

    if risk > 80:
        await publisher.publish_task_failed(
            agent_id=payload.get("owner_id") or "system",
            task_id=payload["task_id"],
            title=payload["task"],
            reason="risk_above_80: human review required before execution",
            source=payload["source"],
            evidence=payload["completion_criteria"],
        )


async def create_or_update_task(conn: asyncpg.Connection, payload: dict[str, Any], defaults: dict[str, Any]) -> tuple[bool, str]:
    risk = int(payload["risk_score"])
    payload["status"] = determine_status(risk)
    payload.setdefault("review_status", "needs_input" if risk > 80 else "pending_review")
    payload.setdefault("refine_status", build_refine_status(risk))
    payload["refined_at"] = None
    payload["retry_count"] = 0

    existing_ref = await conn.fetchrow(
        """
        SELECT id, status
        FROM backlog
        WHERE tenant_id = $3
          AND (source_ref = $1 OR source_fingerprint = $2)
        LIMIT 1
        """,
        payload["source_ref"],
        payload["source_fingerprint"],
        defaults["tenant_id"],
    )

    if existing_ref:
        return False, f"exists:{existing_ref['id']}"

    row = await conn.fetchrow(
        """
        INSERT INTO backlog (
            task,
            priority,
            owner_id,
            lane,
            metadata,
            tenant_id,
            risk_score,
            source,
            source_ref,
            project,
            completion_criteria,
            menu_bucket,
            status,
            review_status,
            refine_status,
            source_fingerprint,
            retry_count
        )
        VALUES (
            $1, $2, $3, $4, $5::jsonb,
            $6, $7, $8, $9, $10,
            $11, $12, $13, $14, $15,
            $16, 0
        )
        RETURNING id, status
        """,
        payload["task"],
        payload["priority"],
        payload["owner_id"],
        payload["lane"],
        json.dumps(payload["metadata"]),
        defaults["tenant_id"],
        payload["risk_score"],
        payload["source"],
        payload["source_ref"],
        payload["project"],
        payload["completion_criteria"],
        payload["menu_bucket"],
        payload["status"],
        payload["review_status"],
        payload["refine_status"],
        payload["source_fingerprint"],
    )

    payload["task_id"] = str(row["id"])
    return True, row["status"]


async def sync_github_issue(task: dict[str, Any], issue_repo: str) -> bool:
    # Best-effort integration; keep failures non-blocking and non-fatal.
    if not issue_repo:
        return False

    title = f"[scan:{task['project']}] {task['task'][:120]}"
    marker_body = (
        f"Source: {task['source_ref']}\n"
        f"Project: {task['project']}\n"
        f"Risk: {task['risk_score']}\n\n"
        f"Completion Criteria:\n{task['completion_criteria']}\n\n"
        f"Metadata:\n{json.dumps(task['metadata'], indent=2)}\n"
    )

    try:
        list_output = subprocess.run(
            ["gh", "issue", "list", "-R", issue_repo, "--state", "open", "--json", "title"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        existing = {j["title"] for j in json.loads(list_output.stdout or "[]")}
        if title in existing:
            return False

        subprocess.run(
            [
                "gh",
                "issue",
                "create",
                "-R",
                issue_repo,
                "--title",
                title,
                "--body",
                marker_body,
                "--label",
                "source:scan",
                "--label",
                f"priority:{task['priority']}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=40,
        )
        return True
    except Exception:
        return False


async def run_scanner() -> int:
    parser = argparse.ArgumentParser(description="Run unified task registry scan")
    parser.add_argument("--repos", type=str, default=None, help="Comma-separated repo roots")
    parser.add_argument("--include", type=str, default=".py,.ts,.tsx,.js,.md,.sh", help="Comma-separated include globs")
    parser.add_argument("--exclude", type=str, default="__pycache__,.pytest_cache,.venv,node_modules,.next,dist,build,target", help="Comma-separated directories/globs")
    parser.add_argument("--owner", type=str, default=None, help="Default task owner")
    parser.add_argument("--menu-bucket", type=str, default="code-scan", help="Menu bucket for task grouping")
    parser.add_argument("--tenant-id", type=str, default=None, help="Tenant UUID override")
    parser.add_argument("--publish-nats", action="store_true", help="Publish scanner events to NATS")
    parser.add_argument("--github-sync", action="store_true", help="Create GH issues for scan tasks")
    parser.add_argument("--github-repo", type=str, default=None, help="Repo for issue sync")
    parser.add_argument("--db-url", type=str, default=None, help="DATABASE_URL override")
    args = parser.parse_args()

    repos = get_repo_roots(args.repos)
    if not repos:
        raise RuntimeError("No repo paths configured; set --repos or TASK_REGISTRY_REPOS")

    db_url = args.db_url or os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL missing")

    tenant_id = args.tenant_id or os.environ.get("TASK_REGISTRY_TENANT_ID") or str(DEFAULT_TENANT_ID)

    include_ext = ",".join([e.strip() for e in args.include.split(",")])
    exclude_globs = [part.strip() for part in args.exclude.split(",") if part.strip()]

    conn = await asyncpg.connect(db_url)
    try:
        await ensure_task_table(conn)
        defaults = {"tenant_id": tenant_id}

        publisher: NATSEventPublisher | None = None
        if args.publish_nats:
            try:
                cluster = NATSClusterManager()
                await cluster.connect()
                publisher = NATSEventPublisher(cluster, gateway_id="task-registry-scanner")
            except Exception:
                publisher = None

        total = 0
        created = 0
        skipped = 0
        github_created = 0

        for repo in repos:
            cmd = build_scan_command(repo, include_ext, exclude_globs)
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            hits = parse_hits(proc.stdout)
            for hit in hits:
                payload = make_task_payload(hit, args.owner, args.menu_bucket)
                passes_refine, reason = refine_candidate(payload)

                if not passes_refine:
                    payload["review_status"] = "needs_input"
                    payload["refine_status"] = "needs_revision"
                    payload["metadata"]["refine_fail_reason"] = reason

                ok, _result = await create_or_update_task(conn, payload, defaults)
                total += 1

                if not ok:
                    skipped += 1
                    continue

                created += 1
                await publish_events(publisher, payload, payload["risk_score"])

                if args.github_sync and args.github_repo:
                    if await sync_github_issue(payload, args.github_repo):
                        github_created += 1

        print(
            json.dumps(
                {
                    "status": "ok",
                    "scanned": total,
                    "created": created,
                    "skipped_existing": skipped,
                    "github_issues": github_created,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            )
        )

        return created
    finally:
        if not conn.is_closed():
            await conn.close()
        if publisher and getattr(publisher, "cluster", None):
            await publisher.cluster.close()


async def _main():
    await run_scanner()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
