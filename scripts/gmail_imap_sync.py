#!/usr/bin/env python3
"""Sync Gmail over IMAP into a self-growing-kb repository.

This adapter is for lightweight personal use: Gmail app password plus IMAP,
with no Google Cloud OAuth client required. It intentionally uses only the
Python standard library.
"""

from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import re
import ssl
import subprocess
import sys
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage, Message
from email.policy import default
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from gmail_candidate_extractor import enrich_event
from gmail_thread_to_event import build_event


DEFAULT_HOST = "imap.gmail.com"
DEFAULT_PORT = 993
DEFAULT_MAILBOX = "INBOX"


class GmailImapError(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GmailImapError(f"File not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise GmailImapError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GmailImapError(f"Expected a JSON object in {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_path(base: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else (base / path).resolve()


def read_secret(root: Path, account: dict[str, Any]) -> str:
    if account.get("app_password"):
        return str(account["app_password"]).replace(" ", "")
    env_var = account.get("password_env")
    if env_var and os.environ.get(str(env_var)):
        return str(os.environ[str(env_var)]).replace(" ", "")
    password_file = resolve_path(root, account.get("password_file"))
    if password_file:
        try:
            return password_file.read_text(encoding="utf-8").strip().replace(" ", "")
        except FileNotFoundError as exc:
            raise GmailImapError(f"Password file not found: {password_file}") from exc
    credential_file = resolve_path(root, account.get("credential_file"))
    if credential_file:
        credential = read_json(credential_file)
        password = credential.get("app_password") or credential.get("password")
        if password:
            return str(password).replace(" ", "")
    raise GmailImapError("Each Gmail IMAP account needs credential_file, password_file, password_env, or app_password")


def parse_fetch_metadata(line: bytes | str) -> dict[str, str]:
    text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
    values = {}
    for name in ("UID", "X-GM-THRID", "X-GM-MSGID"):
        match = re.search(rf"\b{name}\s+([^\s()]+)", text)
        if match:
            values[name.lower().replace("-", "_")] = match.group(1)
    return values


def header_list(message: Message) -> list[dict[str, str]]:
    return [{"name": key, "value": value} for key, value in message.items()]


def first_text(message: Message, body_mode: str) -> str:
    if body_mode == "snippet":
        return snippet_from_message(message)
    if message.is_multipart():
        chunks = []
        for part in message.walk():
            if part.is_multipart():
                continue
            if part.get_content_type() != "text/plain":
                continue
            disposition = str(part.get("Content-Disposition", "")).lower()
            if "attachment" in disposition:
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            chunks.append(payload.decode(charset, errors="replace").strip())
        return "\n\n".join(chunk for chunk in chunks if chunk).strip()
    payload = message.get_payload(decode=True)
    if payload is None:
        return str(message.get_payload() or "").strip()
    charset = message.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace").strip()


def snippet_from_message(message: Message, limit: int = 240) -> str:
    source = first_text(message, "plain")
    compact = " ".join(source.split())
    return compact[:limit]


def internal_date(message: Message) -> str:
    date = message.get("Date")
    if not date:
        return ""
    try:
        parsed = parsedate_to_datetime(str(date))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return str(int(parsed.timestamp() * 1000))
    except (TypeError, ValueError, OverflowError):
        return ""


def fallback_thread_id(message: Message) -> str:
    references = str(message.get("References") or "").split()
    if references:
        return re.sub(r"[^a-zA-Z0-9_.@+-]+", "-", references[0]).strip("-")
    reply_to = str(message.get("In-Reply-To") or "").strip()
    if reply_to:
        return re.sub(r"[^a-zA-Z0-9_.@+-]+", "-", reply_to).strip("-")
    msg_id = str(message.get("Message-ID") or "").strip()
    if msg_id:
        return re.sub(r"[^a-zA-Z0-9_.@+-]+", "-", msg_id).strip("-")
    return "unknown-thread"


def normalize_imap_message(
    raw: bytes,
    metadata: dict[str, str],
    body_mode: str,
) -> dict[str, Any]:
    message = email.message_from_bytes(raw, policy=default)
    text = first_text(message, body_mode)
    uid = metadata.get("uid") or str(abs(hash(raw)))
    message_id = metadata.get("x_gm_msgid") or str(message.get("Message-ID") or uid).strip("<>")
    thread_id = metadata.get("x_gm_thrid") or fallback_thread_id(message)
    return {
        "id": message_id,
        "thread_id": thread_id,
        "label_ids": ["IMAP"],
        "history_id": uid,
        "internal_date": internal_date(message),
        "snippet": snippet_from_message(message),
        "payload": {
            "mime_type": message.get_content_type(),
            "headers": header_list(message),
            "body": {"size": len(raw), "content": text},
            "parts": None,
        },
    }


def grouped_threads(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for message in messages:
        groups.setdefault(str(message["thread_id"]), []).append(message)
    threads = []
    for thread_id, items in groups.items():
        items.sort(key=lambda item: item.get("internal_date") or item.get("history_id") or "")
        threads.append(
            {
                "id": thread_id,
                "history_id": str(items[-1].get("history_id", "")),
                "messages": items,
            }
        )
    return sorted(threads, key=lambda thread: thread.get("history_id", ""), reverse=True)


def imap_since(days: int) -> str:
    date = datetime.now(timezone.utc) - timedelta(days=days)
    return date.strftime("%d-%b-%Y")


def imap_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def imap_search_args(account: dict[str, Any], config: dict[str, Any]) -> list[str]:
    gmail_search = account.get("gmail_search") or config.get("gmail_search")
    if gmail_search:
        return ["X-GM-RAW", imap_quote(str(gmail_search))]
    search = account.get("imap_search") or config.get("imap_search")
    if search:
        return str(search).split()
    since_days = int(account.get("since_days") or config.get("since_days") or 7)
    return ["SINCE", imap_since(since_days), "NOT", "DELETED"]


def connect(account_email: str, password: str, host: str, port: int) -> imaplib.IMAP4_SSL:
    client = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context())
    try:
        client.login(account_email, password)
    except imaplib.IMAP4.error as exc:
        raise GmailImapError(f"IMAP login failed for {account_email}: {exc}") from exc
    return client


def fetch_recent_messages(
    client: imaplib.IMAP4_SSL,
    account: dict[str, Any],
    config: dict[str, Any],
) -> list[tuple[dict[str, str], bytes]]:
    mailbox = str(account.get("mailbox") or config.get("mailbox") or DEFAULT_MAILBOX)
    status, _ = client.select(mailbox, readonly=True)
    if status != "OK":
        raise GmailImapError(f"Could not select IMAP mailbox: {mailbox}")
    status, data = client.uid("SEARCH", *imap_search_args(account, config))
    if status != "OK":
        raise GmailImapError(f"IMAP search failed: {data}")
    uids = (data[0] or b"").split()
    max_messages = int(account.get("max_messages") or config.get("max_messages") or 50)
    selected = uids[-max_messages:]
    fetched = []
    for uid in selected:
        status, parts = client.uid("FETCH", uid, "(UID X-GM-THRID X-GM-MSGID RFC822)")
        if status != "OK":
            continue
        metadata: dict[str, str] = {}
        raw_message = None
        for part in parts:
            if isinstance(part, tuple):
                metadata.update(parse_fetch_metadata(part[0]))
                raw_message = part[1]
            elif isinstance(part, bytes):
                metadata.update(parse_fetch_metadata(part))
        if raw_message:
            fetched.append((metadata, raw_message))
    return fetched


def ingest_event(root: Path, config: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    kb_cli = resolve_path(root, config.get("kb_cli")) or Path(__file__).with_name("kb.py")
    event_dir = root / ".kb" / "connector-runs" / "gmail-imap" / "events"
    event_path = event_dir / f"{event['event_id']}.json"
    write_json(event_path, event)
    result = subprocess.run(
        ["python3", str(kb_cli), "ingest", str(root), str(event_path)],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    return {"returncode": result.returncode, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}


def sync_account(root: Path, config: dict[str, Any], account: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    account_email = str(account["account"])
    password = read_secret(root, account)
    host = str(account.get("host") or config.get("host") or DEFAULT_HOST)
    port = int(account.get("port") or config.get("port") or DEFAULT_PORT)
    body_mode = str(account.get("body_mode") or config.get("body_mode") or "snippet")
    version_mode = str(account.get("version_mode") or config.get("version_mode") or "latest-message-id")
    client = connect(account_email, password, host, port)
    try:
        fetched = fetch_recent_messages(client, account, config)
    finally:
        try:
            client.logout()
        except imaplib.IMAP4.error:
            pass
    messages = [normalize_imap_message(raw, metadata, body_mode) for metadata, raw in fetched]
    results = []
    for thread in grouped_threads(messages):
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
        event["hydration"]["source_adapter"] = "gmail_imap_sync.py"
        event["hydration"]["method"] = "imap"
        event["hydration"]["body_mode"] = body_mode
        event["gmail_imap"] = {
            "account": account_email,
            "host": host,
            "mailbox": str(account.get("mailbox") or config.get("mailbox") or DEFAULT_MAILBOX),
            "search": imap_search_args(account, config),
            "body_mode": body_mode,
        }
        event = enrich_event(event, config, account)
        ingest_result = {"skipped": True, "dry_run": dry_run}
        if not dry_run:
            ingest_result = ingest_event(root, config, event)
        results.append({"thread_id": thread["id"], "event_id": event["event_id"], "ingest": ingest_result})
    return {"account": account_email, "messages_seen": len(messages), "threads_seen": len(results), "results": results}


def sync(args: argparse.Namespace) -> dict[str, Any]:
    root = args.kb_root.resolve()
    config = read_json(args.config.resolve())
    accounts = [account for account in config.get("accounts", []) if account.get("enabled", True)]
    if args.account:
        accounts = [account for account in accounts if account.get("account") == args.account]
    if not accounts:
        raise GmailImapError("No enabled Gmail IMAP accounts matched the sync request")
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
    parsed = argparse.ArgumentParser(description="Gmail IMAP acquisition adapter for self-growing-kb")
    sub = parsed.add_subparsers(dest="command", required=True)
    sync_command = sub.add_parser("sync", help="Sync configured Gmail IMAP accounts into a KB root")
    sync_command.add_argument("--kb-root", type=Path, required=True)
    sync_command.add_argument("--config", type=Path, required=True)
    sync_command.add_argument("--account")
    sync_command.add_argument("--dry-run", action="store_true")
    sync_command.add_argument("--evolve", action="store_true")
    return parsed


def main() -> int:
    try:
        args = parser().parse_args()
        if args.command == "sync":
            print(json.dumps(sync(args), ensure_ascii=False, indent=2))
            return 0
        raise GmailImapError(f"Unknown command: {args.command}")
    except GmailImapError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
