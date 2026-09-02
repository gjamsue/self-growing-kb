#!/usr/bin/env python3
"""Convert a hydrated Gmail thread JSON object into a Normalized Raw Event."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9._@+-]+", "-", value.strip().lower())
    normalized = normalized.strip("-")
    return normalized or "unknown"


def header_value(message: dict[str, Any], name: str) -> str:
    payload = message.get("payload") or {}
    headers = payload.get("headers") or []
    for header in headers:
        if str(header.get("name", "")).lower() == name.lower():
            return str(header.get("value", ""))
    return ""


def plain_text_from_part(part: dict[str, Any]) -> str:
    if part.get("mime_type") == "text/plain":
        body = part.get("body") or {}
        content = body.get("content")
        if content:
            return str(content).replace("\r\n", "\n").strip()
    texts: list[str] = []
    for child in part.get("parts") or []:
        text = plain_text_from_part(child)
        if text:
            texts.append(text)
    return "\n\n".join(texts).strip()


def message_text(message: dict[str, Any]) -> str:
    text = plain_text_from_part(message.get("payload") or {})
    if text:
        return text
    return str(message.get("snippet") or "").strip()


def message_ts(message: dict[str, Any]) -> str:
    date = header_value(message, "Date")
    if date:
        return date
    internal_date = message.get("internal_date")
    if internal_date:
        try:
            timestamp = int(str(internal_date)) / 1000
            return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    return ""


def latest_message(messages: list[dict[str, Any]]) -> dict[str, Any]:
    if not messages:
        raise ValueError("Gmail thread has no messages")
    return messages[-1]


def source_version(thread: dict[str, Any], messages: list[dict[str, Any]], mode: str) -> str:
    latest = latest_message(messages)
    if mode == "thread-history-id":
        value = thread.get("history_id") or latest.get("history_id")
        if not value:
            raise ValueError("thread-history-id requested but no history_id was present")
        return f"history:{value}"
    if mode == "latest-message-id":
        value = latest.get("id")
        if not value:
            raise ValueError("latest-message-id requested but latest message had no id")
        return f"message:{value}"
    if mode == "message-count":
        return f"messages:{len(messages)}"
    raise ValueError(f"Unknown version mode: {mode}")


def participants(messages: list[dict[str, Any]]) -> list[str]:
    values: set[str] = set()
    for message in messages:
        for name in ("From", "To", "Cc", "Bcc"):
            value = header_value(message, name)
            if value:
                values.add(value)
    return sorted(values)


def build_body(thread: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    chunks = []
    for index, message in enumerate(messages, start=1):
        from_value = header_value(message, "From") or "unknown sender"
        subject = header_value(message, "Subject") or thread.get("snippet") or "Gmail thread"
        text = message_text(message)
        chunks.append(
            "\n".join(
                [
                    f"Message {index}",
                    f"From: {from_value}",
                    f"Date: {message_ts(message)}",
                    f"Subject: {subject}",
                    "",
                    text,
                ]
            ).strip()
        )
    return "\n\n---\n\n".join(chunks).strip()


def build_event(args: argparse.Namespace, thread: dict[str, Any]) -> dict[str, Any]:
    messages = list(thread.get("messages") or [])
    if not messages:
        raise ValueError("Gmail thread JSON must contain a non-empty messages array")
    thread_id = str(thread.get("id") or latest_message(messages).get("thread_id") or "")
    if not thread_id:
        raise ValueError("Gmail thread JSON must include a thread id")
    latest = latest_message(messages)
    latest_subject = header_value(latest, "Subject") or "Gmail thread"
    version = source_version(thread, messages, args.version_mode)
    mailbox = slug(args.mailbox_account)
    source_id = f"gmail/{mailbox}/thread/{thread_id}"
    event_id = f"gmail_{slug(args.tenant_id)}_{slug(args.mailbox_account)}_{thread_id}_{slug(version)}"
    display_url = str(args.source_url or f"https://mail.google.com/mail/#all/{latest.get('id', thread_id)}")
    labels = sorted({str(label) for message in messages for label in message.get("label_ids", [])})
    return {
        "event_id": event_id,
        "event_type": args.event_type,
        "tenant_id": args.tenant_id,
        "source_type": "mail",
        "source_id": source_id,
        "source_version": version,
        "source_url": display_url,
        "title": latest_subject,
        "body": build_body(thread, messages),
        "actors": {
            "author": header_value(latest, "From"),
            "participants": participants(messages),
            "owners": [args.mailbox_account],
        },
        "mailbox": {
            "provider": "gmail",
            "account": args.mailbox_account,
            "account_key": mailbox,
            "thread_id": thread_id,
            "latest_message_id": str(latest.get("id") or ""),
            "message_count": len(messages),
            "labels": labels,
        },
        "acl": [
            {
                "principal_type": "user",
                "principal_id": args.principal,
                "permission": "read",
            }
        ],
        "hydration": {
            "hydrated_at": args.hydrated_at or utc_now(),
            "method": "api",
            "quality": "original",
            "adapter": "gmail_thread_to_event.py",
            "version_mode": args.version_mode,
        },
        "evidence_boundary": {
            "proves": [
                "The connected Gmail account exposed this thread snapshot at hydration time.",
                "The thread contained the included headers, timestamps, participants, and plain-text message bodies.",
            ],
            "does_not_prove": [
                "The statements inside the email are factually correct.",
                "The thread is complete outside the connected account's accessible Gmail view.",
            ],
        },
        "knowledge_candidates": [],
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Convert Gmail thread JSON to a Normalized Raw Event")
    root.add_argument("thread_json", type=Path, help="Path to Gmail read_email_thread JSON, or '-' for stdin")
    root.add_argument("--tenant-id", required=True)
    root.add_argument("--principal", required=True)
    root.add_argument("--mailbox-account", required=True, help="Stable Gmail account identity, usually the email")
    root.add_argument("--event-type", choices=("created", "updated", "deleted", "permission_changed"), default="updated")
    root.add_argument(
        "--version-mode",
        choices=("latest-message-id", "thread-history-id", "message-count"),
        default="latest-message-id",
    )
    root.add_argument("--source-url")
    root.add_argument("--hydrated-at")
    return root


def read_thread(path: Path) -> dict[str, Any]:
    raw = sys.stdin.read() if str(path) == "-" else path.read_text(encoding="utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Expected a Gmail thread JSON object")
    return value


def main() -> int:
    try:
        args = parser().parse_args()
        event = build_event(args, read_thread(args.thread_json))
        print(json.dumps(event, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
