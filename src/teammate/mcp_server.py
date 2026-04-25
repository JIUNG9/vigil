"""Minimal MCP (Model Context Protocol) server for the compliance vault.

Implements the JSON-RPC subset Claude Code needs to discover and read
vault content as MCP resources. Stdio transport — Claude Code spawns the
server as a subprocess, talks to it on stdin/stdout.

Resources exposed:

  - ``vault://latest``                       — most recent score summary (latest.md)
  - ``vault://history/<filename>``           — historical run record
  - ``vault://controls/<framework>/<id>``    — per-control evidence
  - ``vault://advisories/<filename>``        — watch-mode advisory diff
  - ``vault://attestations/<filename>``      — signed PDF attestation companion .md

Tools exposed:

  - ``vault.search`` ({"query": str, "k": int}) — keyword/embedding search
    over vault chunks. Returns top-k hits with paths and scores. Caller
    composes the answer themselves; this is a retrieval-only tool.

This file does NOT use any third-party MCP library — we implement the
JSON-RPC framing manually. Reasons:

  1. Zero extra dependencies in the wheel.
  2. The MCP wire protocol is small enough to maintain by hand.
  3. Pinning to a specific MCP SDK version risks compatibility drift.

If a stable Python MCP SDK ships in 2026.x, this module is the natural
swap point. The tool/resource semantics stay the same.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from teammate import __version__
from teammate.rag.ask import retrieve
from teammate.rag.ollama import OllamaClient

PROTOCOL_VERSION = "2025-06-18"  # current MCP spec version at time of writing


# ---------- vault resolution ----------


def _vault_root() -> Path:
    """Resolve compliance-vault/ from CWD or env override."""
    import os

    override = os.environ.get("TEAMMATE_VAULT_ROOT")
    if override:
        return Path(override).resolve()
    return (Path.cwd() / "compliance-vault").resolve()


# ---------- resource enumeration ----------


def _enumerate_resources() -> list[dict[str, Any]]:
    root = _vault_root()
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    latest = root / "latest.md"
    if latest.exists():
        out.append(
            {
                "uri": "vault://latest",
                "name": "Latest compliance score",
                "description": "Most recent teammate score run summary.",
                "mimeType": "text/markdown",
            }
        )
    for sub, name_prefix in (
        ("history", "vault://history/"),
        ("advisories", "vault://advisories/"),
        ("attestations", "vault://attestations/"),
    ):
        d = root / sub
        if d.is_dir():
            for f in sorted(d.glob("*.md")):
                out.append(
                    {
                        "uri": f"{name_prefix}{f.name}",
                        "name": f"{sub.title()}: {f.stem}",
                        "description": f"{sub} record from {f.stem}",
                        "mimeType": "text/markdown",
                    }
                )
    controls_root = root / "controls"
    if controls_root.is_dir():
        for framework_dir in sorted(controls_root.iterdir()):
            if not framework_dir.is_dir():
                continue
            for f in sorted(framework_dir.glob("*.md")):
                out.append(
                    {
                        "uri": f"vault://controls/{framework_dir.name}/{f.stem}",
                        "name": f"{framework_dir.name}:{f.stem}",
                        "description": f"Per-control evidence for {framework_dir.name} {f.stem}",
                        "mimeType": "text/markdown",
                    }
                )
    return out


def _read_resource(uri: str) -> str:
    root = _vault_root()
    if uri == "vault://latest":
        target = root / "latest.md"
    elif uri.startswith("vault://history/"):
        target = root / "history" / uri[len("vault://history/"):]
    elif uri.startswith("vault://advisories/"):
        target = root / "advisories" / uri[len("vault://advisories/"):]
    elif uri.startswith("vault://attestations/"):
        target = root / "attestations" / uri[len("vault://attestations/"):]
    elif uri.startswith("vault://controls/"):
        rest = uri[len("vault://controls/"):]
        framework, _, control_id = rest.partition("/")
        target = root / "controls" / framework / f"{control_id}.md"
    else:
        raise ValueError(f"Unknown vault URI: {uri}")
    if not target.exists():
        raise FileNotFoundError(str(target))
    return target.read_text(encoding="utf-8")


# ---------- tool: vault.search ----------


def _tool_vault_search(args: dict[str, Any]) -> list[dict[str, Any]]:
    query = str(args.get("query", "")).strip()
    k = int(args.get("k", 6))
    if not query:
        return []
    cache_dir = Path.cwd() / ".teammate-cache"
    db = cache_dir / "vault.sqlite"
    if not db.exists():
        return []
    ollama = OllamaClient()
    hits = retrieve(db, query, k=k, ollama=ollama if ollama.is_up() else None)
    return [
        {
            "path": h.path,
            "chunk_idx": h.chunk_idx,
            "framework": h.framework,
            "control": h.control,
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
    """Handle a single JSON-RPC request. Notifications return None."""
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {}) or {}

    if method == "initialize":
        return _make_response(
            req_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {
                    "name": "teammate-vault",
                    "version": __version__,
                },
                "capabilities": {
                    "resources": {"listChanged": False},
                    "tools": {"listChanged": False},
                },
            },
        )
    if method == "initialized":
        return None  # notification — no response

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
        return _make_response(
            req_id,
            {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "text/markdown",
                        "text": text,
                    }
                ]
            },
        )

    if method == "tools/list":
        return _make_response(
            req_id,
            {
                "tools": [
                    {
                        "name": "vault.search",
                        "description": "Search the compliance vault for relevant content. "
                        "Returns top-k chunks with paths and scores.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                                "k": {"type": "integer", "default": 6},
                            },
                            "required": ["query"],
                        },
                    }
                ]
            },
        )

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {}) or {}
        if name == "vault.search":
            hits = _tool_vault_search(args)
            return _make_response(
                req_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"hits": hits}, indent=2),
                        }
                    ],
                    "isError": False,
                },
            )
        return _make_error(req_id, -32601, f"Tool not found: {name}")

    return _make_error(req_id, -32601, f"Method not found: {method}")


def main() -> None:
    """Stdio JSON-RPC loop. Read line-delimited requests, write responses."""
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
        except Exception as exc:  # never crash the loop on a bad request
            resp = _make_error(req.get("id"), -32603, f"Internal error: {exc}")
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
