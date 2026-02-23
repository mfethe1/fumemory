#!/usr/bin/env python3
"""memU Auto-Write Client — Strict memory writing with validation + dedup.

Usage:
    # Direct write
    python memu_auto_write.py write --type fact --agent lenny --confidence 0.9 --content "Memory content here"
    
    # Bulk import from daily memory file
    python memu_auto_write.py sync-daily --agent lenny --file memory/2026-02-23.md
    
    # Search
    python memu_auto_write.py search --query "NATS JetStream"

Environment:
    MEMU_API_URL  — default http://127.0.0.1:8000
    MEMU_API_KEY  — default memu-dev-key
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

import httpx

# --- Config ---

MEMU_API_URL = os.environ.get("MEMU_API_URL", "http://127.0.0.1:8000")
MEMU_API_KEY = os.environ.get("MEMU_API_KEY", "memu-dev-key")

VALID_TYPES = {"fact", "decision", "lesson", "pattern", "failure"}
MIN_CONFIDENCE = 0.7
MIN_CONTENT_LENGTH = 20
MAX_CONTENT_LENGTH = 2000

# --- Validation ---

class MemoryValidationError(Exception):
    pass


def validate_memory(content: str, memory_type: str, confidence: float, agent_id: str) -> dict:
    """Strict validation before writing to memU."""
    errors = []

    if memory_type not in VALID_TYPES:
        errors.append(f"Invalid type '{memory_type}'. Must be one of: {', '.join(sorted(VALID_TYPES))}")

    if confidence < MIN_CONFIDENCE:
        errors.append(f"Confidence {confidence} below minimum {MIN_CONFIDENCE}. Low-confidence memories are noise.")

    if len(content.strip()) < MIN_CONTENT_LENGTH:
        errors.append(f"Content too short ({len(content.strip())} chars). Minimum {MIN_CONTENT_LENGTH}. Be specific.")

    if len(content.strip()) > MAX_CONTENT_LENGTH:
        errors.append(f"Content too long ({len(content.strip())} chars). Maximum {MAX_CONTENT_LENGTH}. Be concise.")

    if not agent_id or len(agent_id) < 2:
        errors.append("Agent ID required (lenny, macklemore, rosie, winnie).")

    # Content quality checks
    vague_phrases = [
        "things are working", "everything is fine", "made progress",
        "did some work", "fixed stuff", "updated things",
    ]
    content_lower = content.lower()
    for phrase in vague_phrases:
        if phrase in content_lower:
            errors.append(f"Vague content detected: '{phrase}'. Be specific about what, when, and why.")

    if errors:
        raise MemoryValidationError("\n".join(f"  - {e}" for e in errors))

    return {
        "content": content.strip(),
        "memory_type": memory_type,
        "agent_id": agent_id,
        "confidence": confidence,
    }


# --- API Client ---

def write_memory(content: str, memory_type: str, agent_id: str, confidence: float = 0.9,
                 metadata: dict | None = None) -> dict:
    """Write a single memory to memU with strict validation."""
    payload = validate_memory(content, memory_type, confidence, agent_id)
    if metadata:
        payload["metadata"] = metadata

    headers = {"X-API-Key": MEMU_API_KEY, "Content-Type": "application/json"}

    with httpx.Client(timeout=30) as client:
        r = client.post(f"{MEMU_API_URL}/memories", headers=headers, json=payload)

        if r.status_code == 200:
            data = r.json()
            action = "deduplicated" if data.get("access_count", 0) > 0 else "created"
            return {"status": "ok", "action": action, "id": data["id"], "access_count": data.get("access_count", 0)}
        else:
            return {"status": "error", "code": r.status_code, "detail": r.text[:200]}


def search_memories(query: str, limit: int = 5) -> list[dict]:
    """Search memU via text search."""
    headers = {"X-API-Key": MEMU_API_KEY}

    with httpx.Client(timeout=30) as client:
        r = client.post(
            f"{MEMU_API_URL}/search-text",
            headers=headers,
            params={"query": query, "limit": limit},
        )
        if r.status_code == 200:
            return r.json()
        return []


def sync_daily_memory(file_path: str, agent_id: str) -> dict:
    """Parse a daily memory markdown file and extract structured memories.
    
    Looks for patterns like:
    - Lines starting with '- **' (key facts/actions)
    - Section headers with timestamps
    - Bullet points with specific outcomes
    """
    if not os.path.exists(file_path):
        return {"status": "error", "detail": f"File not found: {file_path}"}

    with open(file_path) as f:
        content = f.read()

    # Extract meaningful bullet points
    memories_to_write = []
    current_section = ""
    
    for line in content.splitlines():
        line = line.strip()
        
        # Track section context
        if line.startswith("## "):
            current_section = line.lstrip("# ").strip()
            continue
        
        # Extract substantive bullet points
        if line.startswith("- **") and ":" in line:
            # Remove markdown bold markers
            clean = re.sub(r'\*\*', '', line.lstrip("- ")).strip()
            if len(clean) >= MIN_CONTENT_LENGTH:
                # Classify based on content
                memory_type = _classify_content(clean)
                memories_to_write.append({
                    "content": f"[{current_section}] {clean}" if current_section else clean,
                    "memory_type": memory_type,
                    "confidence": 0.85,
                })

    results = {"total": len(memories_to_write), "created": 0, "deduplicated": 0, "errors": 0}
    
    for mem in memories_to_write:
        try:
            result = write_memory(
                content=mem["content"],
                memory_type=mem["memory_type"],
                agent_id=agent_id,
                confidence=mem["confidence"],
            )
            if result["status"] == "ok":
                if result["action"] == "created":
                    results["created"] += 1
                else:
                    results["deduplicated"] += 1
            else:
                results["errors"] += 1
        except MemoryValidationError:
            results["errors"] += 1

    return results


def _classify_content(text: str) -> str:
    """Auto-classify memory type based on content keywords."""
    text_lower = text.lower()
    
    if any(w in text_lower for w in ["lesson", "learned", "mistake", "avoid", "never again"]):
        return "lesson"
    if any(w in text_lower for w in ["decided", "decision", "chose", "architecture decision", "policy"]):
        return "decision"
    if any(w in text_lower for w in ["pattern", "reusable", "template", "always do", "best practice"]):
        return "pattern"
    if any(w in text_lower for w in ["failed", "broke", "regression", "incident", "outage", "anti-pattern"]):
        return "failure"
    return "fact"


# --- CLI ---

def main():
    parser = argparse.ArgumentParser(description="memU Auto-Write Client")
    sub = parser.add_subparsers(dest="command")

    # Write
    wp = sub.add_parser("write", help="Write a single memory")
    wp.add_argument("--type", required=True, choices=sorted(VALID_TYPES))
    wp.add_argument("--agent", required=True)
    wp.add_argument("--confidence", type=float, default=0.9)
    wp.add_argument("--content", required=True)

    # Search
    sp = sub.add_parser("search", help="Search memories")
    sp.add_argument("--query", required=True)
    sp.add_argument("--limit", type=int, default=5)

    # Sync daily
    dp = sub.add_parser("sync-daily", help="Sync daily memory file to memU")
    dp.add_argument("--agent", required=True)
    dp.add_argument("--file", required=True)

    args = parser.parse_args()

    if args.command == "write":
        try:
            result = write_memory(args.content, args.type, args.agent, args.confidence)
            print(json.dumps(result, indent=2))
        except MemoryValidationError as e:
            print(f"Validation failed:\n{e}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "search":
        results = search_memories(args.query, args.limit)
        for r in results:
            print(f"[{r.get('memory_type', '?')}] (conf={r.get('confidence', '?')}) {r.get('content', '')[:120]}")

    elif args.command == "sync-daily":
        result = sync_daily_memory(args.file, args.agent)
        print(json.dumps(result, indent=2))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
