"""Tool definitions + dispatch for the memU MCP server.

Every coding agent — Claude Code, Cursor, Aider, Zed, Continue, Codex CLI,
Gemini CLI, raw LangGraph — sees the same tool names and schemas through
this module. Editor plugins are thin config; logic lives here.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from ..rlm import Orchestrator, RLMContext
from ..storage.base import LinkRecord, StorageBackend, WikiNode
from ..wiki import parse_wikilinks
from .neighborhood_lock import InProcessLockBackend, NeighborhoodLock


_DEFAULT_LOCK_BACKEND = InProcessLockBackend()


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "wiki_search",
        "description": (
            "Full-text search across the memU wiki. Returns ranked slugs + "
            "snippets. Use this first when a task references an unfamiliar "
            "codebase concept, paper, or prior decision."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                "kind": {
                    "type": "string",
                    "enum": ["note", "code", "paper", "task"],
                    "description": "Optional node-kind filter.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "wiki_read",
        "description": "Fetch a single node by slug or id, including outbound links.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Slug or ULID/UUID."},
                "include_links": {"type": "boolean", "default": True},
            },
            "required": ["ref"],
        },
    },
    {
        "name": "wiki_write",
        "description": (
            "Create or update a wiki node. Wikilinks in the body are parsed "
            "and recorded as outbound edges."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": ["note", "code", "paper", "task"],
                    "default": "note",
                },
                "tags": {"type": "array", "items": {"type": "string"}},
                "agent_id": {"type": "string", "default": "mcp"},
            },
            "required": ["slug", "body"],
        },
    },
    {
        "name": "wiki_link",
        "description": "Add a typed edge between two existing slugs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "src_slug": {"type": "string"},
                "dst_slug": {"type": "string"},
                "type": {
                    "type": "string",
                    "enum": [
                        "similar",
                        "extends",
                        "contradicts",
                        "supersedes",
                        "caused_by",
                        "related",
                    ],
                    "default": "related",
                },
                "strength": {
                    "type": "number",
                    "default": 0.5,
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
            },
            "required": ["src_slug", "dst_slug"],
        },
    },
    {
        "name": "wiki_backlinks",
        "description": "List inbound + outbound links for a node.",
        "inputSchema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}},
            "required": ["ref"],
        },
    },
    {
        "name": "rlm_solve",
        "description": (
            "Run the Karpathy-style recursive orchestrator. Decomposes the "
            "task, retrieves relevant slugs from the wiki, and recursively "
            "summarizes. Returns cited slugs + a synthesis."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "max_depth": {"type": "integer", "default": 2, "minimum": 1, "maximum": 5},
                "k": {"type": "integer", "default": 6, "minimum": 1, "maximum": 20},
                "budget": {"type": "integer", "default": 8000, "minimum": 512},
            },
            "required": ["task"],
        },
    },
    {
        "name": "wiki_ingest_code",
        "description": (
            "Walk a source tree and materialize one wiki node per "
            "top-level symbol (class, function, method, module). Imports "
            "and same-module calls become typed outbound links. "
            "Idempotent: re-running on an unchanged tree is a no-op."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Filesystem path to the source root.",
                },
                "languages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Restrict to these parser keys (e.g. ['python']).",
                },
                "includes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Glob patterns to include (repeatable).",
                },
                "excludes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Glob patterns to exclude (repeatable).",
                },
                "respect_gitignore": {"type": "boolean", "default": True},
                "force": {"type": "boolean", "default": False},
                "repo_url": {"type": "string"},
                "commit_sha": {"type": "string"},
            },
            "required": ["path"],
        },
    },
]


async def dispatch(
    backend: StorageBackend, name: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Execute a tool call and return a serializable result.

    The MCP server wraps the output in a protocol-level envelope; this
    function stays protocol-agnostic so the same dispatcher powers HTTP,
    LangGraph adapters, and the CLI.
    """
    if name == "wiki_search":
        hits = await backend.search_fts(
            args["query"], k=args.get("k", 10), kind=args.get("kind")
        )
        return {
            "hits": [
                {
                    "slug": h.node.slug,
                    "title": h.node.title,
                    "kind": h.node.kind,
                    "score": h.score,
                    "snippet": _snippet(h.node.body, args["query"]),
                }
                for h in hits
            ]
        }
    if name == "wiki_read":
        node = await backend.get_node(args["ref"])
        if node is None:
            return {"error": f"not found: {args['ref']}"}
        out: dict[str, Any] = {
            "id": node.id,
            "slug": node.slug,
            "kind": node.kind,
            "title": node.title,
            "body": node.body,
            "tags": node.tags,
        }
        if args.get("include_links", True):
            out["outbound"] = [
                {"dst_slug": l.dst_slug, "type": l.type, "strength": l.strength}
                for l in await backend.list_links(node.slug, direction="out")
            ]
            out["inbound"] = [
                {"src_slug": l.src_slug, "type": l.type, "strength": l.strength}
                for l in await backend.list_links(node.slug, direction="in")
            ]
        return out
    if name == "wiki_write":
        body = args["body"]
        new_outbound = [link.slug for link in parse_wikilinks(body)]
        # Neighborhood: the target slug + any slug this write links out to.
        # Target lock serializes concurrent writes to this slug; outbound locks
        # prevent racing against concurrent writes to slugs we're linking to.
        neighborhood = {args["slug"], *new_outbound}
        async with NeighborhoodLock(_DEFAULT_LOCK_BACKEND, neighborhood):
            existing = await backend.get_node(args["slug"])
            node_id = existing.id if existing else str(uuid.uuid4())
            links = [
                LinkRecord(src_slug=args["slug"], dst_slug=dst, type="related")
                for dst in new_outbound
            ]
            node = WikiNode(
                id=node_id,
                slug=args["slug"],
                kind=args.get("kind", "note"),
                title=args.get("title") or args["slug"],
                body=body,
                tags=args.get("tags", []),
                agent_id=args.get("agent_id", "mcp"),
                links=links,
                created_at=existing.created_at if existing else datetime.now(timezone.utc),
            )
            await backend.put_node(node)
        return {"id": node.id, "slug": node.slug, "outbound_links": len(links)}
    if name == "wiki_link":
        link = LinkRecord(
            src_slug=args["src_slug"],
            dst_slug=args["dst_slug"],
            type=args.get("type", "related"),
            strength=args.get("strength", 0.5),
        )
        await backend.put_link(link)
        return {"ok": True}
    if name == "wiki_backlinks":
        outbound = await backend.list_links(args["ref"], direction="out")
        inbound = await backend.list_links(args["ref"], direction="in")
        return {
            "outbound": [
                {"dst_slug": l.dst_slug, "type": l.type, "strength": l.strength}
                for l in outbound
            ],
            "inbound": [
                {"src_slug": l.src_slug, "type": l.type, "strength": l.strength}
                for l in inbound
            ],
        }
    if name == "rlm_solve":
        orch = Orchestrator(backend)
        ctx = RLMContext(
            max_depth=args.get("max_depth", 2),
            k=args.get("k", 6),
            token_budget=args.get("budget", 8000),
        )
        result = await orch.solve(args["task"], ctx)
        return {
            "task": result.task,
            "answer": result.answer,
            "slugs": result.slugs,
            "depth": result.depth,
        }
    if name == "wiki_ingest_code":
        from ..ingest import ingest_codebase

        result = await ingest_codebase(
            backend,
            args["path"],
            languages=args.get("languages"),
            includes=args.get("includes"),
            excludes=args.get("excludes"),
            respect_gitignore=args.get("respect_gitignore", True),
            commit_sha=args.get("commit_sha"),
            repo_url=args.get("repo_url"),
            force=args.get("force", False),
        )
        return {
            "added": result.added,
            "updated": result.updated,
            "unchanged_count": len(result.unchanged),
            "scanned_files": result.scanned_files,
            "skipped_files": result.skipped_files,
            "parse_errors": result.parse_errors,
            "elapsed_ms": result.elapsed_ms,
            "commit_sha": result.commit_sha,
        }
    raise ValueError(f"Unknown tool: {name!r}")


def _snippet(body: str, query: str, width: int = 160) -> str:
    if not body:
        return ""
    idx = body.lower().find(query.lower())
    if idx < 0:
        return body[:width].strip()
    start = max(0, idx - width // 2)
    end = min(len(body), idx + width // 2)
    return body[start:end].strip()
