from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Iterable
from urllib import error, request

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_QUERY = "heartbeat test"
DEFAULT_LIMIT = 1
KEY_FILES = (
    Path.home() / ".openclaw/secrets/memu_api_key",
    Path(__file__).resolve().parents[1] / ".env",
)


def _iter_env_lines(path: Path) -> Iterable[str]:
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        yield line


def resolve_api_key() -> str | None:
    direct = os.environ.get("MEMU_API_KEY")
    if direct:
        return direct.strip()

    for path in KEY_FILES:
        if not path.exists():
            continue
        if path.name == ".env":
            for line in _iter_env_lines(path):
                if line.startswith("MEMU_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        else:
            value = path.read_text().strip()
            if value:
                return value
    return None


def _request_json(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, body: dict | None = None) -> tuple[int, dict | list | str]:
    data = None
    req_headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        req_headers.setdefault("Content-Type", "application/json")
    req = request.Request(url, method=method, headers=req_headers, data=data)
    try:
        with request.urlopen(req, timeout=15) as resp:
            payload = resp.read().decode()
            return resp.status, json.loads(payload) if payload else {}
    except error.HTTPError as exc:
        payload = exc.read().decode()
        parsed = payload
        try:
            parsed = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            pass
        return exc.code, parsed


def run_smoke(base_url: str, *, query: str = DEFAULT_QUERY, limit: int = DEFAULT_LIMIT) -> dict:
    base = base_url.rstrip("/")
    health_status, health_body = _request_json(f"{base}/health")

    key = resolve_api_key()
    headers = {"X-MemU-Key": key} if key else {}
    search_status, search_body = _request_json(
        f"{base}/search/recall",
        method="POST",
        headers=headers,
        body={"query": query, "limit": limit},
    )

    fallback_status = None
    fallback_body = None
    if search_status == 401 and key:
        fallback_status, fallback_body = _request_json(
            f"{base}/search-text?query={query.replace(' ', '%20')}&limit={limit}",
            method="POST",
            headers=headers,
        )

    ok = health_status == 200 and (search_status == 200 or fallback_status == 200)
    return {
        "ok": ok,
        "base_url": base,
        "health": {"status": health_status, "body": health_body},
        "search_recall": {"status": search_status, "body": search_body},
        "search_text": {"status": fallback_status, "body": fallback_body},
        "api_key_source_found": bool(key),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a memU smoke check with auth fallback.")
    parser.add_argument("--base-url", default=os.environ.get("MEMU_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()

    result = run_smoke(args.base_url, query=args.query, limit=args.limit)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
