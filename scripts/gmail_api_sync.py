#!/usr/bin/env python3
"""Sync Gmail API threads into a self-growing-kb repository.

This script intentionally uses only the Python standard library so the Gmail
adapter can run in lightweight agent harnesses, cron jobs, or local shells.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from argparse import Namespace
from pathlib import Path
from typing import Any

from gmail_candidate_extractor import enrich_event
from gmail_thread_to_event import build_event


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailApiError(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GmailApiError(f"File not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise GmailApiError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GmailApiError(f"Expected a JSON object in {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_path(base: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else (base / path).resolve()


def oauth_client(path: Path) -> dict[str, str]:
    raw = read_json(path)
    client = raw.get("installed") or raw.get("web") or raw
    client_id = client.get("client_id")
    client_secret = client.get("client_secret")
    if not client_id or not client_secret:
        raise GmailApiError("OAuth client JSON must include client_id and client_secret")
    return {
        "client_id": str(client_id),
        "client_secret": str(client_secret),
    }


def http_json(
    url: str,
    *,
    method: str = "GET",
    access_token: str | None = None,
    form: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    data = None
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        method = "POST"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise GmailApiError(f"HTTP {exc.code} from {url}: {body[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise GmailApiError(f"Request failed for {url}: {exc}") from exc


def authorization_url(client_secret_file: Path, redirect_uri: str, state: str, scopes: list[str]) -> str:
    client = oauth_client(client_secret_file)
    query = urllib.parse.urlencode(
        {
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"{AUTH_URL}?{query}"


def exchange_code(client_secret_file: Path, code: str, redirect_uri: str) -> dict[str, Any]:
    client = oauth_client(client_secret_file)
    token = http_json(
        TOKEN_URL,
        form={
            "code": code,
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    if "expires_in" in token:
        token["expires_at"] = int(time.time()) + int(token["expires_in"])
    return token


def refresh_token(root: Path, account: dict[str, Any]) -> str:
    token_path = resolve_path(root, account.get("token_file"))
    client_path = resolve_path(root, account.get("client_secret_file"))
    if not token_path or not client_path:
        raise GmailApiError("Each Gmail account needs token_file and client_secret_file")
    token = read_json(token_path)
    access_token = token.get("access_token")
    expires_at = int(token.get("expires_at", 0) or 0)
    if access_token and expires_at > int(time.time()) + 60:
        return str(access_token)
    refresh = token.get("refresh_token")
    if not refresh:
        raise GmailApiError(f"Token file has no refresh_token: {token_path}")
    client = oauth_client(client_path)
    refreshed = http_json(
        TOKEN_URL,
        form={
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "refresh_token": str(refresh),
            "grant_type": "refresh_token",
        },
    )
    token.update(refreshed)
    if "expires_in" in refreshed:
        token["expires_at"] = int(time.time()) + int(refreshed["expires_in"])
    write_json(token_path, token)
    return str(token["access_token"])


def gmail_get(access_token: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    query = urllib.parse.urlencode({key: value for key, value in (params or {}).items() if value is not None})
    url = f"{GMAIL_API}/{path.lstrip('/')}"
    if query:
        url = f"{url}?{query}"
    return http_json(url, access_token=access_token)


def decode_base64url(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8", errors="replace")


def normalize_part(part: dict[str, Any], body_mode: str) -> dict[str, Any]:
    body = part.get("body") or {}
    content = ""
    if body_mode == "plain" and body.get("data"):
        content = decode_base64url(str(body["data"]))
    normalized = {
        "part_id": str(part.get("partId", part.get("part_id", ""))),
        "mime_type": str(part.get("mimeType", part.get("mime_type", ""))),
        "filename": str(part.get("filename", "")),
        "headers": part.get("headers", []),
        "body": {
            "size": body.get("size", 0),
            "content": content,
            "attachment_id": body.get("attachmentId"),
        },
        "parts": None,
    }
    children = part.get("parts") or []
    if children:
        normalized["parts"] = [normalize_part(child, body_mode) for child in children]
    return normalized


def normalize_message(message: dict[str, Any], body_mode: str) -> dict[str, Any]:
    normalized = {
        "id": str(message.get("id", "")),
        "thread_id": str(message.get("threadId", message.get("thread_id", ""))),
        "label_ids": list(message.get("labelIds", message.get("label_ids", []))),
        "history_id": str(message.get("historyId", message.get("history_id", ""))),
        "internal_date": str(message.get("internalDate", message.get("internal_date", ""))),
        "snippet": str(message.get("snippet", "")),
        "payload": normalize_part(message.get("payload") or {}, body_mode),
    }
    if body_mode == "snippet":
        normalized["payload"]["body"]["content"] = normalized["snippet"]
    return normalized


def normalize_thread(thread: dict[str, Any], body_mode: str) -> dict[str, Any]:
    return {
        "id": str(thread.get("id", "")),
        "history_id": str(thread.get("historyId", thread.get("history_id", ""))),
        "messages": [normalize_message(message, body_mode) for message in thread.get("messages", [])],
    }


def gmail_threads(access_token: str, query: str, max_threads: int) -> list[dict[str, Any]]:
    threads: list[dict[str, Any]] = []
    page_token = None
    while len(threads) < max_threads:
        response = gmail_get(
            access_token,
            "threads",
            {
                "q": query,
                "maxResults": min(100, max_threads - len(threads)),
                "pageToken": page_token,
            },
        )
        threads.extend(response.get("threads", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return threads[:max_threads]


def sync_account(root: Path, config: dict[str, Any], account: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    account_email = str(account["account"])
    query = str(account.get("query") or config.get("query") or "newer_than:7d -in:spam -in:trash -category:promotions")
    max_threads = int(account.get("max_threads") or config.get("max_threads") or 30)
    version_mode = str(account.get("version_mode") or config.get("version_mode") or "latest-message-id")
    body_mode = str(account.get("body_mode") or config.get("body_mode") or "snippet")
    access_token = refresh_token(root, account)
    listed = gmail_threads(access_token, query, max_threads)
    results = []
    for item in listed:
        thread_id = item.get("id")
        if not thread_id:
            continue
        raw_thread = gmail_get(access_token, f"threads/{thread_id}", {"format": "full"})
        thread = normalize_thread(raw_thread, body_mode)
        event = build_event(
            Namespace(
                tenant_id=config["tenant_id"],
                principal=config["principal"],
                mailbox_account=account_email,
                event_type="updated",
                version_mode=version_mode,
                source_url=None,
                hydrated_at=None,
            ),
            thread,
        )
        event["hydration"]["source_adapter"] = "gmail_api_sync.py"
        event["hydration"]["body_mode"] = body_mode
        event["gmail_api"] = {
            "query": query,
            "thread_id": thread_id,
            "account": account_email,
            "body_mode": body_mode,
        }
        event = enrich_event(event, config, account)
        ingest_result = {"skipped": True, "dry_run": dry_run}
        if not dry_run:
            ingest_result = ingest_event(root, config, event)
        results.append({"thread_id": thread_id, "event_id": event["event_id"], "ingest": ingest_result})
    return {"account": account_email, "query": query, "threads_seen": len(listed), "results": results}


def ingest_event(root: Path, config: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    kb_cli = resolve_path(root, config.get("kb_cli"))
    if not kb_cli:
        kb_cli = Path(__file__).with_name("kb.py")
    event_dir = root / ".kb" / "connector-runs" / "gmail-api" / "events"
    event_dir.mkdir(parents=True, exist_ok=True)
    event_path = event_dir / f"{event['event_id']}.json"
    write_json(event_path, event)
    result = subprocess.run(
        ["python3", str(kb_cli), "ingest", str(root), str(event_path)],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def sync(args: argparse.Namespace) -> dict[str, Any]:
    root = args.kb_root.resolve()
    config = read_json(args.config.resolve())
    accounts = [account for account in config.get("accounts", []) if account.get("enabled", True)]
    if args.account:
        accounts = [account for account in accounts if account.get("account") == args.account]
    if not accounts:
        raise GmailApiError("No enabled Gmail accounts matched the sync request")
    results = [sync_account(root, config, account, args.dry_run) for account in accounts]
    if args.evolve and not args.dry_run:
        kb_cli = resolve_path(root, config.get("kb_cli")) or Path(__file__).with_name("kb.py")
        evolved = subprocess.run(
            ["python3", str(kb_cli), "evolve", str(root)],
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
        return {"accounts": results, "evolve": {"returncode": evolved.returncode, "stdout": evolved.stdout, "stderr": evolved.stderr}}
    return {"accounts": results}


def parser() -> argparse.ArgumentParser:
    parsed = argparse.ArgumentParser(description="Gmail API acquisition adapter for self-growing-kb")
    sub = parsed.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth-url", help="Print a user-consent URL for a Gmail OAuth client")
    auth.add_argument("--client-secret-file", type=Path, required=True)
    auth.add_argument("--redirect-uri", default="http://localhost:8765/callback")
    auth.add_argument("--state", default="gmail-api-sync")
    auth.add_argument("--scope", action="append", default=[GMAIL_READONLY_SCOPE])

    exchange = sub.add_parser("exchange-code", help="Exchange an OAuth code for a token JSON file")
    exchange.add_argument("--client-secret-file", type=Path, required=True)
    exchange.add_argument("--code", required=True)
    exchange.add_argument("--redirect-uri", default="http://localhost:8765/callback")
    exchange.add_argument("--token-file", type=Path, required=True)

    sync_command = sub.add_parser("sync", help="Sync configured Gmail accounts into a KB root")
    sync_command.add_argument("--kb-root", type=Path, required=True)
    sync_command.add_argument("--config", type=Path, required=True)
    sync_command.add_argument("--account")
    sync_command.add_argument("--dry-run", action="store_true")
    sync_command.add_argument("--evolve", action="store_true")
    return parsed


def main() -> int:
    try:
        args = parser().parse_args()
        if args.command == "auth-url":
            print(authorization_url(args.client_secret_file.resolve(), args.redirect_uri, args.state, args.scope))
            return 0
        if args.command == "exchange-code":
            token = exchange_code(args.client_secret_file.resolve(), args.code, args.redirect_uri)
            write_json(args.token_file.resolve(), token)
            print(json.dumps({"status": "token_written", "token_file": str(args.token_file.resolve())}, indent=2))
            return 0
        if args.command == "sync":
            print(json.dumps(sync(args), ensure_ascii=False, indent=2))
            return 0
        raise GmailApiError(f"Unknown command: {args.command}")
    except GmailApiError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
