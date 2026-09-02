#!/usr/bin/env python3
"""Read-only MCP server for a self-growing-kb repository.

This intentionally stays stdlib-only so the same code can run in Codex, Claude,
or a small personal host without a Python package install step.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from kb_core import (
    KBError,
    SearchDocument,
    extract_title,
    iter_documents,
    list_proposals,
    load_config,
    normalize_timestamp,
    read_json,
    revision_for_time,
    revision_path,
    score_text,
    status_kb,
)


SERVER_NAME = "self-growing-kb"
PROTOCOL_VERSION = "2025-06-18"
DEFAULT_LIMIT = 8
MAX_LIMIT = 50
MAX_FILE_BYTES = 256_000

READABLE_PREFIXES = (
    "00_Index",
    "01_Raw/events",
    "02_Wiki",
    "03_Query",
    "04_Promote/proposals",
    "04_Promote/approved",
    "04_Promote/rejected",
    ".kb/pages",
    ".kb/revisions",
)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def clamp_limit(value: Any, default: int = DEFAULT_LIMIT) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(MAX_LIMIT, parsed))


def relative_to_root(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError as exc:
        raise KBError(f"Path is outside the wiki root: {path}") from exc


def validate_readable_path(root: Path, relative_path: str) -> Path:
    clean = relative_path.strip().lstrip("/")
    if not clean:
        raise KBError("path is required")
    candidate = (root / clean).resolve()
    relative = relative_to_root(root, candidate)
    if ".." in Path(relative).parts:
        raise KBError("Path traversal is not allowed")
    if not any(relative == prefix or relative.startswith(f"{prefix}/") for prefix in READABLE_PREFIXES):
        raise KBError(f"Path is not readable through this MCP server: {relative}")
    if not candidate.is_file():
        raise KBError(f"File not found: {relative}")
    if candidate.stat().st_size > MAX_FILE_BYTES:
        raise KBError(f"File is too large to fetch through MCP: {relative}")
    return candidate


def short_file(path: Path) -> Any:
    if path.suffix == ".json":
        return read_json(path)
    return path.read_text(encoding="utf-8")


def document_result(item: SearchDocument) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "id": item.identifier,
        "title": item.title,
        "path": item.path,
        "score": item.score,
        "excerpt": re.sub(r"\s+", " ", item.text).strip()[:600],
        "sources": item.sources,
        "revision_id": item.revision_id,
        "valid_from": item.valid_from,
    }


class WikiMCP:
    def __init__(self, root: Path, principal: str) -> None:
        self.root = root.resolve()
        self.principal = principal

    def tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "search",
                "description": "Search the personal wiki and permitted raw evidence. Alias of wiki_search.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Question or keywords to search for."},
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
                        "wiki_only": {"type": "boolean"},
                        "as_of": {"type": "string", "description": "Optional ISO-8601 timestamp for historical lookup."},
                        "include_history": {"type": "boolean"},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "fetch",
                "description": "Fetch a wiki page, raw event, proposal, revision, or safe repository path. Alias of wiki_fetch.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["wiki", "raw", "proposal", "revision", "path"],
                        },
                        "id": {"type": "string"},
                        "path": {"type": "string"},
                        "page_id": {"type": "string"},
                        "revision_id": {"type": "string"},
                        "as_of": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "wiki_search",
                "description": "Search the wiki without mutating traces, usage counters, or gaps.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "principal": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
                        "wiki_only": {"type": "boolean"},
                        "as_of": {"type": "string"},
                        "include_history": {"type": "boolean"},
                    },
                    "required": ["question"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "wiki_fetch",
                "description": "Fetch one readable wiki object by id or repository-relative path.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["wiki", "raw", "proposal", "revision", "path"],
                        },
                        "id": {"type": "string"},
                        "path": {"type": "string"},
                        "page_id": {"type": "string"},
                        "revision_id": {"type": "string"},
                        "as_of": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "wiki_recent_changes",
                "description": "List recent committed changes in the wiki repository.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
                        "since": {"type": "string", "description": "Optional git --since value, such as '7 days ago'."},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "wiki_list_proposals",
                "description": "List knowledge update proposals by review status.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["pending", "blocked", "approved", "rejected"],
                        },
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "wiki_status",
                "description": "Return repository health, queue counts, page counts, and proposal counts.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        ]

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "search":
            query = str(arguments.get("query", "")).strip()
            return self.search(
                query,
                str(arguments.get("principal") or self.principal),
                clamp_limit(arguments.get("limit")),
                not bool(arguments.get("wiki_only", False)),
                arguments.get("as_of"),
                bool(arguments.get("include_history", False)),
            )
        if name == "wiki_search":
            return self.search(
                str(arguments.get("question", "")).strip(),
                str(arguments.get("principal") or self.principal),
                clamp_limit(arguments.get("limit")),
                not bool(arguments.get("wiki_only", False)),
                arguments.get("as_of"),
                bool(arguments.get("include_history", False)),
            )
        if name in {"fetch", "wiki_fetch"}:
            return self.fetch(arguments)
        if name == "wiki_recent_changes":
            return self.recent_changes(clamp_limit(arguments.get("limit")), arguments.get("since"))
        if name == "wiki_list_proposals":
            return self.proposals(arguments.get("status"), clamp_limit(arguments.get("limit")))
        if name == "wiki_status":
            return status_kb(self.root)
        raise KBError(f"Unknown tool: {name}")

    def search(
        self,
        question: str,
        principal: str,
        limit: int,
        include_raw: bool,
        as_of: Any,
        include_history: bool,
    ) -> dict[str, Any]:
        if not question:
            raise KBError("question is required")
        config = load_config(self.root)
        scored: list[SearchDocument] = []
        for document in iter_documents(
            self.root,
            principal,
            include_raw,
            config["profile"],
            as_of=str(as_of) if as_of else None,
            include_history=include_history,
        ):
            score = score_text(question, document.title, document.text)
            if score > 0:
                scored.append(SearchDocument(**{**document.__dict__, "score": score}))
        scored.sort(key=lambda item: (-item.score, item.kind, item.identifier))
        selected = scored[:limit]
        return {
            "question": question,
            "principal": principal,
            "as_of": normalize_timestamp(str(as_of)) if as_of else None,
            "include_history": include_history,
            "include_raw": include_raw,
            "confidence": "medium" if selected else "low",
            "evidence": [document_result(item) for item in selected],
            "gaps": [] if selected else ["No matching evidence was found."],
        }

    def fetch(self, arguments: dict[str, Any]) -> dict[str, Any]:
        kind = str(arguments.get("kind") or ("path" if arguments.get("path") else "wiki"))
        if arguments.get("path"):
            path = validate_readable_path(self.root, str(arguments["path"]))
            return {"kind": "path", "path": relative_to_root(self.root, path), "content": short_file(path)}
        if kind == "wiki":
            return self.fetch_wiki(arguments)
        if kind == "raw":
            return self.fetch_raw(str(arguments.get("id") or ""))
        if kind == "proposal":
            return self.fetch_proposal(str(arguments.get("id") or ""))
        if kind == "revision":
            return self.fetch_revision(arguments)
        raise KBError(f"Unsupported fetch kind: {kind}")

    def fetch_wiki(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id = str(arguments.get("id") or arguments.get("page_id") or "").strip()
        if not page_id:
            raise KBError("wiki fetch requires id or page_id")
        registry_path = validate_readable_path(self.root, f".kb/pages/{page_id}.json")
        registry = read_json(registry_path)
        revision_id = revision_for_time(registry, str(arguments["as_of"]) if arguments.get("as_of") else None)
        if not revision_id:
            raise KBError(f"No revision found for page_id: {page_id}")
        revision = read_json(revision_path(self.root, page_id, revision_id))
        page_path = validate_readable_path(self.root, str(registry["path"]))
        return {
            "kind": "wiki",
            "id": page_id,
            "title": extract_title(str(revision["markdown"]), page_id),
            "path": relative_to_root(self.root, page_path),
            "revision_id": revision_id,
            "valid_from": revision.get("valid_from"),
            "evidence": revision.get("evidence", []),
            "markdown": revision["markdown"],
        }

    def fetch_raw(self, identifier: str) -> dict[str, Any]:
        if not identifier.strip():
            raise KBError("raw fetch requires id")
        raw_root = self.root / "01_Raw" / "events"
        candidates = [raw_root / f"{identifier}.json"]
        candidates.extend(path for path in raw_root.glob("*.json") if path.stem != identifier)
        for path in candidates:
            if not path.exists():
                continue
            event = read_json(validate_readable_path(self.root, str(path.relative_to(self.root))))
            if path.stem == identifier or event.get("event_id") == identifier:
                return {"kind": "raw", "id": event.get("event_id"), "path": str(path.relative_to(self.root)), "event": event}
        raise KBError(f"Raw event not found: {identifier}")

    def fetch_proposal(self, proposal_id: str) -> dict[str, Any]:
        if not proposal_id.strip():
            raise KBError("proposal fetch requires id")
        for directory in ("proposals", "approved", "rejected"):
            path = self.root / "04_Promote" / directory / f"{proposal_id}.json"
            if path.exists():
                return {
                    "kind": "proposal",
                    "id": proposal_id,
                    "path": str(path.relative_to(self.root)),
                    "proposal": read_json(validate_readable_path(self.root, str(path.relative_to(self.root)))),
                }
        raise KBError(f"Proposal not found: {proposal_id}")

    def fetch_revision(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page_id = str(arguments.get("page_id") or "").strip()
        revision_id = str(arguments.get("revision_id") or arguments.get("id") or "").strip()
        if "@" in revision_id and not page_id:
            page_id, revision_id = revision_id.split("@", 1)
        if not page_id or not revision_id:
            raise KBError("revision fetch requires page_id and revision_id, or id as page_id@revision_id")
        path = validate_readable_path(self.root, f".kb/revisions/{page_id}/{revision_id}.json")
        return {"kind": "revision", "page_id": page_id, "revision_id": revision_id, "revision": read_json(path)}

    def proposals(self, status: Any, limit: int) -> dict[str, Any]:
        status_value = str(status) if status else None
        if status_value and status_value not in {"pending", "blocked", "approved", "rejected"}:
            raise KBError(f"Unsupported proposal status: {status_value}")
        proposals = list_proposals(self.root, status_value)
        proposals.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return {"status": status_value, "count": len(proposals), "proposals": proposals[:limit]}

    def recent_changes(self, limit: int, since: Any) -> dict[str, Any]:
        command = [
            "git",
            "-C",
            str(self.root),
            "log",
            f"-n{limit}",
            "--date=iso-strict",
            "--pretty=format:%H%x00%ad%x00%s",
            "--name-only",
        ]
        if since:
            command.insert(4, f"--since={since}")
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise KBError(result.stderr.strip() or "git log failed")
        commits: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for line in result.stdout.splitlines():
            if "\x00" in line:
                commit, authored_at, subject = line.split("\x00", 2)
                current = {"commit": commit, "authored_at": authored_at, "subject": subject, "paths": []}
                commits.append(current)
                continue
            if current is not None and line.strip():
                current["paths"].append(line.strip())
        return {"count": len(commits), "commits": commits}


def success_response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def mcp_text_result(value: Any) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json_dumps(value)}],
        "structuredContent": value if isinstance(value, dict) else {"result": value},
    }


def handle_json_rpc(server: WikiMCP, request: dict[str, Any]) -> dict[str, Any] | None:
    request_id = request.get("id")
    method = str(request.get("method", ""))
    params = request.get("params") if isinstance(request.get("params"), dict) else {}

    if request_id is None:
        return None
    try:
        if method == "initialize":
            return success_response(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": "0.1.0"},
                    "instructions": "Read-only access to a self-growing Markdown knowledge base.",
                },
            )
        if method == "ping":
            return success_response(request_id, {})
        if method == "tools/list":
            return success_response(request_id, {"tools": server.tools()})
        if method == "tools/call":
            name = str(params.get("name", ""))
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            return success_response(request_id, mcp_text_result(server.call(name, arguments)))
        return error_response(request_id, -32601, f"Method not found: {method}")
    except KBError as exc:
        return error_response(request_id, -32000, str(exc))
    except Exception as exc:  # pragma: no cover - keeps HTTP server alive on unexpected errors.
        return error_response(request_id, -32603, f"Internal error: {exc}")


class MCPHandler(BaseHTTPRequestHandler):
    server_version = "SelfGrowingKBMCP/0.1"

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json(HTTPStatus.OK, {"status": "ok", "server": SERVER_NAME})
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/mcp":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        token = getattr(self.server, "mcp_token", "")
        if token:
            expected = f"Bearer {token}"
            if self.headers.get("Authorization") != expected:
                self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            self.send_json(HTTPStatus.BAD_REQUEST, error_response(None, -32700, "Invalid JSON"))
            return

        wiki_server = getattr(self.server, "wiki_mcp")
        if isinstance(payload, list):
            responses = [handle_json_rpc(wiki_server, item) for item in payload if isinstance(item, dict)]
            responses = [item for item in responses if item is not None]
            self.send_json(HTTPStatus.OK if responses else HTTPStatus.ACCEPTED, responses)
            return
        if not isinstance(payload, dict):
            self.send_json(HTTPStatus.BAD_REQUEST, error_response(None, -32600, "Invalid Request"))
            return
        response = handle_json_rpc(wiki_server, payload)
        if response is None:
            self.send_response(HTTPStatus.ACCEPTED)
            self.end_headers()
            return
        self.send_json(HTTPStatus.OK, response)

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def send_json(self, status: HTTPStatus, value: Any) -> None:
        encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Run a read-only MCP server for a self-growing-kb repository")
    cli.add_argument("--wiki-root", type=Path, default=Path(os.environ.get("WIKI_ROOT", ".")))
    cli.add_argument("--principal", default=os.environ.get("WIKI_PRINCIPAL", "personal-gjamsue"))
    cli.add_argument("--host", default=os.environ.get("WIKI_MCP_HOST", "127.0.0.1"))
    cli.add_argument("--port", type=int, default=int(os.environ.get("WIKI_MCP_PORT", "8766")))
    cli.add_argument("--token", default=os.environ.get("WIKI_MCP_TOKEN", ""))
    return cli


def main() -> int:
    args = parser().parse_args()
    wiki_root = args.wiki_root.resolve()
    try:
        load_config(wiki_root)
    except KBError as exc:
        print(json_dumps({"error": str(exc), "wiki_root": str(wiki_root)}), file=sys.stderr)
        return 2
    httpd = ThreadingHTTPServer((args.host, args.port), MCPHandler)
    httpd.wiki_mcp = WikiMCP(wiki_root, args.principal)  # type: ignore[attr-defined]
    httpd.mcp_token = args.token  # type: ignore[attr-defined]
    print(
        json_dumps(
            {
                "status": "listening",
                "endpoint": f"http://{args.host}:{args.port}/mcp",
                "wiki_root": str(wiki_root),
                "auth": "bearer" if args.token else "none",
            }
        ),
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
