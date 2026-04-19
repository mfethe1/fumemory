"""memU command-line entrypoint.

Subcommands::

    memu init <path>                 # initialize a new vault
    memu put <slug> [--file body.md] # write a node
    memu get <slug>                  # print a node
    memu search <query>              # FTS search
    memu links <slug>                # list inbound + outbound links
    memu solve <task>                # run RLM orchestrator
    memu serve mcp                   # start the MCP server (stdio)

The CLI is backend-agnostic; it reads ``MEMU_STORAGE_DSN`` to pick
markdown, SQLite, or Postgres.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..rlm import Orchestrator, RLMContext
from ..storage import StorageBackend, get_backend
from ..storage.base import WikiNode
from ..storage.markdown_backend import slugify
from ..wiki import parse_wikilinks
from ..wiki.vault import VaultLayout


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memu", description="memU vault CLI")
    p.add_argument(
        "--dsn",
        default=None,
        help="Storage DSN (overrides MEMU_STORAGE_DSN).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", help="Initialize a new vault directory")
    init.add_argument("path")

    put = sub.add_parser("put", help="Write a node")
    put.add_argument("slug")
    put.add_argument("--title", default=None)
    put.add_argument("--kind", default="note", choices=["note", "code", "paper", "task"])
    put.add_argument("--file", default=None, help="Read body from file ('-' for stdin)")
    put.add_argument("--body", default=None, help="Inline body text")

    get = sub.add_parser("get", help="Print a node")
    get.add_argument("slug")

    search = sub.add_parser("search", help="Full-text search")
    search.add_argument("query")
    search.add_argument("-k", type=int, default=10)

    links = sub.add_parser("links", help="Show inbound + outbound links")
    links.add_argument("slug")

    solve = sub.add_parser("solve", help="Run the RLM orchestrator")
    solve.add_argument("task")
    solve.add_argument("--max-depth", type=int, default=2)
    solve.add_argument("--k", type=int, default=6)

    serve = sub.add_parser("serve", help="Start a server")
    serve.add_argument("kind", choices=["mcp"])

    ingest = sub.add_parser("ingest", help="Ingest external sources")
    ingest_sub = ingest.add_subparsers(dest="ingest_kind", required=True)
    ingest_code = ingest_sub.add_parser("code", help="Ingest a codebase")
    ingest_code.add_argument("path", help="Path to the source root to ingest")
    ingest_code.add_argument(
        "--language",
        action="append",
        default=None,
        help="Restrict to a language (repeatable). Default: all supported.",
    )
    ingest_code.add_argument(
        "--include",
        action="append",
        default=None,
        help="Glob to include (repeatable).",
    )
    ingest_code.add_argument(
        "--exclude",
        action="append",
        default=None,
        help="Glob to exclude (repeatable).",
    )
    ingest_code.add_argument(
        "--no-gitignore",
        action="store_true",
        help="Do not honor .gitignore files during the walk.",
    )
    ingest_code.add_argument(
        "--force",
        action="store_true",
        help="Re-write all nodes even when the source hash matches.",
    )
    ingest_code.add_argument(
        "--repo-url", default=None, help="Record a repo URL in each node's source."
    )
    ingest_code.add_argument(
        "--commit", default=None, help="Override the detected commit SHA."
    )

    return p


async def _cmd_init(args: argparse.Namespace) -> int:
    path = Path(os.path.expanduser(args.path)).resolve()
    layout = VaultLayout(path)
    layout.ensure()
    print(f"Initialized vault at {path}")
    print(f"  notes/  {layout.notes}")
    print(f"  code/   {layout.code}")
    print(f"  papers/ {layout.papers}")
    print(f"  tasks/  {layout.tasks}")
    print(f"Set MEMU_STORAGE_DSN=file://{path} to use it.")
    return 0


async def _cmd_put(args: argparse.Namespace, backend: StorageBackend) -> int:
    if args.file == "-":
        body = sys.stdin.read()
    elif args.file:
        body = Path(args.file).read_text(encoding="utf-8")
    elif args.body is not None:
        body = args.body
    else:
        body = f"# {args.title or args.slug}\n"
    existing = await backend.get_node(args.slug)
    node_id = existing.id if existing else str(uuid.uuid4())
    links = []
    for link in parse_wikilinks(body):
        from ..storage.base import LinkRecord

        links.append(LinkRecord(src_slug=args.slug, dst_slug=link.slug, type="related"))
    node = WikiNode(
        id=node_id,
        slug=args.slug,
        kind=args.kind,
        title=args.title or args.slug,
        body=body,
        links=links,
        created_at=existing.created_at if existing else datetime.now(timezone.utc),
    )
    await backend.put_node(node)
    print(f"Wrote {args.slug} -> id={node_id}")
    return 0


async def _cmd_get(args: argparse.Namespace, backend: StorageBackend) -> int:
    node = await backend.get_node(args.slug)
    if node is None:
        print(f"Not found: {args.slug}", file=sys.stderr)
        return 1
    print(json.dumps(node.to_frontmatter(), indent=2, default=str))
    print("---")
    print(node.body)
    return 0


async def _cmd_search(args: argparse.Namespace, backend: StorageBackend) -> int:
    hits = await backend.search_fts(args.query, k=args.k)
    for h in hits:
        print(f"{h.score:6.2f}  [[{h.node.slug}]]  {h.node.title}")
    return 0


async def _cmd_links(args: argparse.Namespace, backend: StorageBackend) -> int:
    out = await backend.list_links(args.slug, direction="out")
    inbound = await backend.list_links(args.slug, direction="in")
    print(f"outbound ({len(out)}):")
    for l in out:
        print(f"  -> [[{l.dst_slug}]]  ({l.type}, s={l.strength})")
    print(f"inbound ({len(inbound)}):")
    for l in inbound:
        print(f"  <- [[{l.src_slug}]]  ({l.type}, s={l.strength})")
    return 0


async def _cmd_solve(args: argparse.Namespace, backend: StorageBackend) -> int:
    orch = Orchestrator(backend)
    result = await orch.solve(
        args.task, RLMContext(max_depth=args.max_depth, k=args.k)
    )
    print(f"Task: {result.task}")
    print(f"Cited slugs: {result.slugs}")
    print("---")
    print(result.answer)
    return 0


async def _cmd_serve(args: argparse.Namespace, backend: StorageBackend) -> int:
    if args.kind == "mcp":
        from ..mcp.server import run_stdio

        await run_stdio(backend)
        return 0
    return 1


async def _cmd_ingest_code(args: argparse.Namespace, backend: StorageBackend) -> int:
    from ..ingest import ingest_codebase

    result = await ingest_codebase(
        backend,
        args.path,
        languages=args.language,
        includes=args.include,
        excludes=args.exclude,
        respect_gitignore=not args.no_gitignore,
        commit_sha=args.commit,
        repo_url=args.repo_url,
        force=args.force,
    )
    print(f"Ingested {args.path}")
    print(f"  {result.summary()}")
    if result.commit_sha:
        print(f"  commit={result.commit_sha[:12]}")
    if result.added:
        print(f"  added:     {len(result.added)} (first 5: {result.added[:5]})")
    if result.updated:
        print(f"  updated:   {len(result.updated)} (first 5: {result.updated[:5]})")
    return 0


async def _dispatch(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "init":
        return await _cmd_init(args)
    backend = get_backend(args.dsn)
    await backend.init()
    try:
        if args.cmd == "put":
            return await _cmd_put(args, backend)
        if args.cmd == "get":
            return await _cmd_get(args, backend)
        if args.cmd == "search":
            return await _cmd_search(args, backend)
        if args.cmd == "links":
            return await _cmd_links(args, backend)
        if args.cmd == "solve":
            return await _cmd_solve(args, backend)
        if args.cmd == "serve":
            return await _cmd_serve(args, backend)
        if args.cmd == "ingest":
            if args.ingest_kind == "code":
                return await _cmd_ingest_code(args, backend)
    finally:
        await backend.close()
    return 1


def main(argv: Optional[list[str]] = None) -> int:
    try:
        return asyncio.run(_dispatch(argv))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
