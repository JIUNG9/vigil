"""Minimal MCP (Model Context Protocol) server for the team brain.

Exposes the team's git-repo-of-markdown to Claude Code as MCP resources +
one search tool. Stdio JSON-RPC. Zero third-party deps.

Resources:

  - ``brain://CLAUDE.md``                — the team's global rules file
  - ``brain://skills/<name>``            — a team-specific skill (.claude/skills/<name>/SKILL.md)
  - ``brain://rules/<name>``             — a split-out rules file (.claude/rules/<name>.md)
  - ``brain://docs/<path>``              — anything under docs/
  - ``brain://knowledge/<path>``         — anything under knowledge/

Tools:

  - ``brain.search`` ({"query": str, "k": int}) — top-k hits from the
    sqlite-vec index, with paths and scores. Use this when Claude needs
    to retrieve relevant chunks before answering a contextual question.

Implementation choice: hand-rolled JSON-RPC, no MCP SDK. Keeps the wheel
tiny and avoids version drift.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from teammate import __version__
from teammate.rag.ask import retrieve
from teammate.rag.ollama import OllamaClient

PROTOCOL_VERSION = "2025-06-18"


# ---------- brain root resolution ----------


def _brain_root() -> Path:
    """Resolve the team brain root from env override or CWD."""
    override = os.environ.get("TEAMMATE_BRAIN_ROOT")
    if override:
        return Path(override).resolve()
    return Path.cwd().resolve()


# ---------- resource enumeration ----------


def _enumerate_resources() -> list[dict[str, Any]]:
    root = _brain_root()
    out: list[dict[str, Any]] = []

    claude_md = root / "CLAUDE.md"
    if claude_md.exists():
        out.append({
            "uri": "brain://CLAUDE.md",
            "name": "CLAUDE.md",
            "description": "Team's global rules file (loaded by Claude Code at session start).",
            "mimeType": "text/markdown",
        })

    skills_root = root / ".claude" / "skills"
    if skills_root.is_dir():
        for skill_dir in sorted(skills_root.iterdir()):
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                out.append({
                    "uri": f"brain://skills/{skill_dir.name}",
                    "name": f"Skill: {skill_dir.name}",
                    "description": f"Team-specific skill at .claude/skills/{skill_dir.name}/SKILL.md",
                    "mimeType": "text/markdown",
                })

    rules_root = root / ".claude" / "rules"
    if rules_root.is_dir():
        for rule_md in sorted(rules_root.glob("*.md")):
            out.append({
                "uri": f"brain://rules/{rule_md.stem}",
                "name": f"Rule: {rule_md.stem}",
                "description": f"Split-out rules file at .claude/rules/{rule_md.name}",
                "mimeType": "text/markdown",
            })

    for top in ("docs", "knowledge"):
        top_dir = root / top
        if not top_dir.is_dir():
            continue
        for md in sorted(top_dir.rglob("*.md")):
            rel = md.relative_to(top_dir).as_posix()
            out.append({
                "uri": f"brain://{top}/{rel}",
                "name": f"{top}/{rel}",
                "description": f"{top} entry: {rel}",
                "mimeType": "text/markdown",
            })

    return out


def _read_resource(uri: str) -> str:
    root = _brain_root()

    if uri == "brain://CLAUDE.md":
        target = root / "CLAUDE.md"
    elif uri.startswith("brain://skills/"):
        name = uri[len("brain://skills/"):]
        target = root / ".claude" / "skills" / name / "SKILL.md"
    elif uri.startswith("brain://rules/"):
        name = uri[len("brain://rules/"):]
        target = root / ".claude" / "rules" / f"{name}.md"
    elif uri.startswith("brain://docs/"):
        rel = uri[len("brain://docs/"):]
        target = root / "docs" / rel
    elif uri.startswith("brain://knowledge/"):
        rel = uri[len("brain://knowledge/"):]
        target = root / "knowledge" / rel
    else:
        raise ValueError(f"Unknown brain URI: {uri}")

    if not target.exists():
        raise FileNotFoundError(str(target))
    return target.read_text(encoding="utf-8")


# ---------- tool: brain.search ----------


def _tool_brain_search(args: dict[str, Any]) -> list[dict[str, Any]]:
    query = str(args.get("query", "")).strip()
    k = int(args.get("k", 6))
    if not query:
        return []
    cache_dir = _brain_root() / ".teammate-cache"
    db = cache_dir / "vault.sqlite"
    if not db.exists():
        return []
    ollama = OllamaClient()
    hits = retrieve(db, query, k=k, ollama=ollama if ollama.is_up() else None)
    return [
        {
            "path": h.path,
            "chunk_idx": h.chunk_idx,
            "section": h.kind,
            "score": h.score,
            "text": h.text,
        }
        for h in hits
    ]


# ---------- JSON-RPC dispatch ----------


def _make_response(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _make_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


def handle(req: dict[str, Any]) -> dict[str, Any] | None:
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {}) or {}

    if method == "initialize":
        return _make_response(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": {"name": "teammate-brain", "version": __version__},
            "capabilities": {
                "resources": {"listChanged": False},
                "tools": {"listChanged": False},
            },
        })
    if method == "initialized":
        return None

    if method == "resources/list":
        return _make_response(req_id, {"resources": _enumerate_resources()})

    if method == "resources/read":
        uri = params.get("uri", "")
        try:
            text = _read_resource(uri)
        except FileNotFoundError:
            return _make_error(req_id, -32602, f"Resource not found: {uri}")
        except ValueError as exc:
            return _make_error(req_id, -32602, str(exc))
        return _make_response(req_id, {
            "contents": [{"uri": uri, "mimeType": "text/markdown", "text": text}]
        })

    if method == "tools/list":
        return _make_response(req_id, {
            "tools": [{
                "name": "brain.search",
                "description": "Search the team brain. Returns top-k chunks with paths and scores.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "k": {"type": "integer", "default": 6},
                    },
                    "required": ["query"],
                },
            }]
        })

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {}) or {}
        if name == "brain.search":
            hits = _tool_brain_search(args)
            return _make_response(req_id, {
                "content": [{"type": "text", "text": json.dumps({"hits": hits}, indent=2)}],
                "isError": False,
            })
        return _make_error(req_id, -32601, f"Tool not found: {name}")

    return _make_error(req_id, -32601, f"Method not found: {method}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            resp = handle(req)
        except Exception as exc:
            resp = _make_error(req.get("id"), -32603, f"Internal error: {exc}")
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
