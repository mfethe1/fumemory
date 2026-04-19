"""YAML-frontmatter parser compatible with Obsidian.

Kept dependency-light: pulls in ``PyYAML`` when available, falls back to a
minimal parser for the subset of YAML memU emits. Every write round-trips
through Obsidian unchanged.
"""
from __future__ import annotations

from typing import Any

try:  # Optional: PyYAML is already a transitive dep via fastapi stacks.
    import yaml  # type: ignore

    _HAS_YAML = True
except Exception:  # pragma: no cover - fallback path
    yaml = None
    _HAS_YAML = False


_DELIM = "---"


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split ``text`` into (frontmatter_dict, body).

    If no frontmatter is present, returns ``({}, text)``.
    """
    if not text.startswith(_DELIM):
        return {}, text
    lines = text.splitlines(keepends=True)
    if not lines:
        return {}, text
    # First line must be exactly ---
    if lines[0].strip() != _DELIM:
        return {}, text
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _DELIM:
            end_idx = i
            break
    if end_idx is None:
        return {}, text
    raw_fm = "".join(lines[1:end_idx])
    body = "".join(lines[end_idx + 1 :])
    if body.startswith("\n"):
        body = body[1:]
    fm = _load_yaml(raw_fm)
    if not isinstance(fm, dict):
        fm = {}
    return fm, body


def dump_frontmatter(fm: dict[str, Any], body: str) -> str:
    """Serialize a frontmatter dict + body back to a markdown file."""
    raw_fm = _dump_yaml(fm)
    if not raw_fm.endswith("\n"):
        raw_fm += "\n"
    if not body.endswith("\n"):
        body += "\n"
    return f"{_DELIM}\n{raw_fm}{_DELIM}\n{body}"


# ---------------------------------------------------------------------------
# YAML (de)serialization with safe fallback.


def _load_yaml(text: str) -> Any:
    if _HAS_YAML:
        return yaml.safe_load(text) or {}
    return _naive_load(text)


def _dump_yaml(data: Any) -> str:
    if _HAS_YAML:
        return yaml.safe_dump(
            data, sort_keys=False, allow_unicode=True, default_flow_style=False
        )
    return _naive_dump(data)


# ---- Minimal fallback ------------------------------------------------------
# Handles only the subset emitted by memU: flat scalar keys, lists of dicts
# under ``links``, list of strings under ``tags``. Intentionally conservative.


def _naive_load(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if not val:
            # Could be a nested list/dict.
            items: list[Any] = []
            dict_map: dict[str, Any] = {}
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if not nxt.startswith("  "):
                    break
                stripped = nxt.strip()
                if stripped.startswith("- "):
                    items.append(_parse_scalar(stripped[2:]))
                elif ":" in stripped:
                    k2, _, v2 = stripped.partition(":")
                    dict_map[k2.strip()] = _parse_scalar(v2.strip())
                j += 1
            if items:
                out[key] = items
            elif dict_map:
                out[key] = dict_map
            else:
                out[key] = None
            i = j
            continue
        out[key] = _parse_scalar(val)
        i += 1
    return out


def _parse_scalar(val: str) -> Any:
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(x.strip()) for x in inner.split(",")]
    if val in {"null", "None", "~", ""}:
        return None
    if val in {"true", "True"}:
        return True
    if val in {"false", "False"}:
        return False
    if (val.startswith('"') and val.endswith('"')) or (
        val.startswith("'") and val.endswith("'")
    ):
        return val[1:-1]
    try:
        if "." in val:
            return float(val)
        return int(val)
    except ValueError:
        return val


def _naive_dump(data: Any, indent: int = 0) -> str:
    lines: list[str] = []
    pad = "  " * indent
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.append(_naive_dump(v, indent + 1))
            else:
                lines.append(f"{pad}{k}: {_dump_scalar(v)}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                first = True
                for k, v in item.items():
                    prefix = f"{pad}- " if first else f"{pad}  "
                    lines.append(f"{prefix}{k}: {_dump_scalar(v)}")
                    first = False
            else:
                lines.append(f"{pad}- {_dump_scalar(item)}")
    else:
        lines.append(f"{pad}{_dump_scalar(data)}")
    return "\n".join(lines)


def _dump_scalar(val: Any) -> str:
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, list):
        return "[" + ", ".join(_dump_scalar(x) for x in val) + "]"
    s = str(val)
    if any(ch in s for ch in ":#[]{},&*!|>'\"%@`"):
        return '"' + s.replace('"', '\\"') + '"'
    return s
