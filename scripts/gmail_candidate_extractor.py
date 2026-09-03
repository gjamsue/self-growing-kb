#!/usr/bin/env python3
"""Extract conservative wiki candidates from Gmail Raw Events."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gmail_thread_to_event import slug
from kb_core import event_key


DEFAULT_VERSION = "gmail-rules-v1"
DEFAULT_VALUE_KEYWORDS = [
    "alina",
    "jocelyn",
    "miss west",
    "school",
    "montessori",
    "teacher",
    "visa",
    "passport",
    "vfs",
    "vfsglobal",
    "fedex",
    "shipment",
    "tracking",
    "tracking number",
    "appointment",
    "schedule",
    "meeting",
    "deadline",
    "application",
    "document",
    "invoice",
    "receipt",
    "payment",
    "travel",
    "flight",
    "hotel",
    "personal wiki",
    "knowledge base",
]
DEFAULT_SKIP_KEYWORDS = [
    "unsubscribe",
    "marketing",
    "newsletter",
    "promotion",
    "discount",
    "sale",
    "security alert",
    "verification code",
    "one-time code",
    "password reset",
    "daily digest",
]


class GmailCandidateError(RuntimeError):
    pass


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GmailCandidateError(f"File not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise GmailCandidateError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GmailCandidateError(f"Expected a JSON object in {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_path(base: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else (base / path).resolve()


def merged_extraction_config(config: dict[str, Any], account: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = dict(config.get("candidate_extraction") or {})
    if account and isinstance(account.get("candidate_extraction"), dict):
        merged.update(account["candidate_extraction"])
    return merged


def extraction_enabled(config: dict[str, Any], account: dict[str, Any] | None = None) -> bool:
    extraction = merged_extraction_config(config, account)
    return bool(extraction.get("enabled"))


def extraction_version(config: dict[str, Any], account: dict[str, Any] | None = None) -> str:
    extraction = merged_extraction_config(config, account)
    return str(extraction.get("version") or DEFAULT_VERSION)


def keyword_list(extraction: dict[str, Any], key: str, defaults: list[str]) -> list[str]:
    values = extraction.get(key)
    if not values:
        return defaults
    return [str(value) for value in values if str(value).strip()]


def compact(value: str, limit: int = 320) -> str:
    cleaned = " ".join(value.split())
    return cleaned[:limit].strip()


def matched_keywords(text: str, keywords: list[str]) -> list[str]:
    lowered = text.lower()
    return sorted({keyword for keyword in keywords if keyword.lower() in lowered})


def rule_terms(rule: dict[str, Any], key: str) -> list[str]:
    values = rule.get(key) or []
    return [str(value).strip().lower() for value in values if str(value).strip()]


def priority_rule_for(text: str, extraction: dict[str, Any]) -> dict[str, Any] | None:
    lowered = text.lower()
    for rule in extraction.get("priority_rules") or []:
        if not isinstance(rule, dict):
            continue
        all_terms = rule_terms(rule, "keywords_all")
        any_terms = rule_terms(rule, "keywords_any")
        none_terms = rule_terms(rule, "keywords_none")
        if none_terms and any(term in lowered for term in none_terms):
            continue
        if all_terms and not all(term in lowered for term in all_terms):
            continue
        if any_terms and not any(term in lowered for term in any_terms):
            continue
        if all_terms or any_terms:
            return rule
    return None


def category_for(text: str) -> tuple[str, str, list[str]]:
    lowered = text.lower()
    rules = [
        ("family/school", "Family school mail", ["alina", "jocelyn", "miss west", "school", "montessori", "teacher", "classroom"]),
        ("admin/visa", "Visa and documents mail", ["visa", "passport", "vfs", "vfsglobal", "application", "document"]),
        ("admin/shipments", "Shipment mail", ["fedex", "shipment", "tracking", "delivery", "delivered"]),
        ("calendar/appointments", "Appointment and schedule mail", ["appointment", "schedule", "meeting", "deadline"]),
        ("finance/records", "Financial record mail", ["invoice", "receipt", "payment", "billing"]),
        ("projects/personal-wiki", "Personal wiki mail", ["personal wiki", "knowledge base", "codex", "github"]),
        ("travel/plans", "Travel planning mail", ["travel", "flight", "hotel", "booking"]),
    ]
    for topic, label, keywords in rules:
        hits = [keyword for keyword in keywords if keyword in lowered]
        if hits:
            return topic, label, hits
    return "mail/notable", "Notable mail", []


def date_hint(event: dict[str, Any]) -> str:
    effective = str(event.get("effective_at") or "")
    if effective:
        return effective[:10]
    hydrated = str((event.get("hydration") or {}).get("hydrated_at") or "")
    if hydrated:
        return hydrated[:10]
    return utc_today()


def has_date_hint(text: str) -> bool:
    numeric = r"\b(?:20\d{2}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]20\d{2})\b"
    month_name = r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+20\d{2}\b"
    weekday_date = r"\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+20\d{2}\b"
    return bool(re.search(numeric, text, re.I) or re.search(month_name, text, re.I) or re.search(weekday_date, text, re.I))


def has_action_hint(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:deadline|due|appointment|meeting|schedule|scheduled|confirm|pickup|submit|renew|book|ship(?:ped|ment)?|tracking|delivery|delivered)\b",
            text,
            re.I,
        )
    )


def shipment_tracking_number(text: str) -> str | None:
    lowered = text.lower()
    if not any(term in lowered for term in ("fedex", "shipment", "tracking", "delivery", "delivered")):
        return None
    for match in re.finditer(r"\b\d{10,22}\b", text):
        value = match.group(0)
        if not value.startswith("20"):
            return value
    return None


def safe_page_id(value: str) -> str:
    safe = re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower())
    safe = re.sub(r"-{2,}", "-", safe).strip("-._")
    return safe[:128].strip("-._") or "gmail-note"


def rule_tags(rule: dict[str, Any], fallback: list[str]) -> list[str]:
    tags = [str(tag) for tag in rule.get("tags") or [] if str(tag).strip()]
    return tags or fallback


def replacement_markdown(
    *,
    page_id: str,
    title: str,
    summary: str,
    principal: str,
    event_id: str,
    tags: list[str],
) -> str:
    today = utc_today()
    tag_text = ", ".join(slug(tag) for tag in tags if tag)
    return (
        "---\n"
        f"id: {page_id}\n"
        "type: wiki\n"
        "status: active\n"
        "visibility: restricted\n"
        f"allowed_principals: [{principal}]\n"
        f"created: {today}\n"
        f"updated: {today}\n"
        f"tags: [{tag_text}]\n"
        f"sources: [{event_id}]\n"
        "---\n\n"
        f"# {title}\n\n"
        "## Current Note\n\n"
        f"{summary}\n\n"
        "## Use Carefully\n\n"
        "This note was extracted from Gmail evidence. Treat it as a candidate memory until reviewed; it does not prove that every statement in the email is true or still current.\n\n"
        "## Evidence\n\n"
        f"- {event_id}\n"
    )


def extract_candidates(
    event: dict[str, Any],
    config: dict[str, Any],
    account: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    extraction = merged_extraction_config(config, account)
    if not extraction.get("enabled"):
        return []
    text = f"{event.get('title', '')}\n{event.get('body', '')}"
    priority_rule = priority_rule_for(text, extraction)
    skip_hits = matched_keywords(text, keyword_list(extraction, "skip_keywords", DEFAULT_SKIP_KEYWORDS))
    if skip_hits and not priority_rule:
        return []
    value_hits = matched_keywords(text, keyword_list(extraction, "value_keywords", DEFAULT_VALUE_KEYWORDS))
    if not value_hits and not priority_rule:
        return []
    has_date = has_date_hint(text)
    has_action = has_action_hint(text)
    score = len(value_hits) + int(has_date) + int(has_action)
    min_score = int(extraction.get("min_score") or 2)
    if score < min_score and not priority_rule:
        return []

    topic_prefix, category_label, category_hits = category_for(text)
    if priority_rule:
        topic_prefix = str(priority_rule.get("topic_prefix") or topic_prefix)
        category_label = str(priority_rule.get("label") or category_label)
    title = compact(str(event.get("title") or "Gmail thread"), 96)
    tracking_number = shipment_tracking_number(text)
    configured_page_id = str(priority_rule.get("page_id") or "") if priority_rule else ""
    configured_topic_key = str(priority_rule.get("topic_key") or "") if priority_rule else ""
    if configured_page_id:
        page_id = safe_page_id(configured_page_id)
    elif tracking_number:
        page_id = f"gmail-shipment-{tracking_number}"
    else:
        page_id = safe_page_id(f"gmail-{slug(topic_prefix.split('/')[-1])}-{slug(title)[:72]}")
    if configured_topic_key:
        topic_key = configured_topic_key
    elif tracking_number:
        topic_key = f"{topic_prefix}/tracking-{tracking_number}"
    else:
        topic_key = f"{topic_prefix}/{slug(title)}"
    evidence = [str(event["event_id"])]
    match_reason = ", ".join(value_hits[:8]) or ", ".join(category_hits) or "configured priority rule"
    summary = (
        f'Gmail thread "{title}" appears relevant to {category_label.lower()} '
        f"because it matched {match_reason}. "
        f"Snippet: {compact(str(event.get('body') or ''), 260)}"
    )
    confidence = str(priority_rule.get("confidence")) if priority_rule and priority_rule.get("confidence") else ("medium" if score >= int(extraction.get("medium_score") or 3) else "low")
    principal = str(config.get("principal") or "unknown")
    tags = rule_tags(priority_rule or {}, [topic_prefix.split("/")[0], topic_prefix.split("/")[-1], "gmail"])
    memory_type = str(priority_rule.get("memory_type") or "event") if priority_rule else "event"
    return [
        {
            "memory_type": memory_type,
            "topic_key": topic_key,
            "claim_key": f"{topic_key}:{event.get('source_version')}",
            "page_id": page_id,
            "title": f"{category_label}: {title}",
            "summary": summary,
            "valid_from": f"{date_hint(event)}T00:00:00Z",
            "replacement_markdown": replacement_markdown(
                page_id=page_id,
                title=f"{category_label}: {title}",
                summary=summary,
                principal=principal,
                event_id=str(event["event_id"]),
                tags=tags,
            ),
            "permission_scope": principal,
            "confidence": confidence,
            "evidence": evidence,
            "extraction": {
                "version": extraction_version(config, account),
                "score": score,
                "keyword_hits": value_hits,
                "priority_rule": priority_rule.get("name") if priority_rule else None,
                "skip_hits_ignored": skip_hits if skip_hits and priority_rule else [],
            },
        }
    ]


def source_version_with_extractor(source_version: str, version: str) -> str:
    marker = f"+candidate:{version}"
    return source_version if source_version.endswith(marker) else f"{source_version}{marker}"


def enrich_event(
    event: dict[str, Any],
    config: dict[str, Any],
    account: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not extraction_enabled(config, account):
        return event
    if event.get("knowledge_candidates"):
        return event
    version = extraction_version(config, account)
    enriched = dict(event)
    enriched["source_version"] = source_version_with_extractor(str(event["source_version"]), version)
    enriched["event_id"] = f"{event['event_id']}_candidate_{slug(version)}"
    hydration = dict(enriched.get("hydration") or {})
    hydration["candidate_extractor"] = version
    enriched["hydration"] = hydration
    enriched["knowledge_candidates"] = extract_candidates(enriched, config, account)
    return enriched if enriched["knowledge_candidates"] else event


def ingest_event(root: Path, config: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    kb_cli = resolve_path(root, config.get("kb_cli")) or Path(__file__).with_name("kb.py")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(event, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        event_path = Path(handle.name)
    try:
        result = subprocess.run(
            ["python3", str(kb_cli), "ingest", str(root), str(event_path)],
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
        return {"returncode": result.returncode, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
    finally:
        event_path.unlink(missing_ok=True)


def event_exists(root: Path, event: dict[str, Any]) -> bool:
    return (root / "01_Raw" / "events" / f"{event_key(event)}.json").exists()


def backfill(args: argparse.Namespace) -> dict[str, Any]:
    root = args.kb_root.resolve()
    config = read_json(args.config.resolve())
    account_by_email = {
        str(account.get("account")): account
        for account in config.get("accounts", [])
        if account.get("enabled", True)
    }
    results = []
    for path in sorted((root / "01_Raw" / "events").glob("*.json")):
        event = read_json(path)
        if event.get("source_type") != "mail":
            continue
        mailbox = event.get("mailbox") or {}
        if mailbox.get("provider") != "gmail":
            continue
        account = account_by_email.get(str(mailbox.get("account")))
        if not account:
            continue
        enriched = enrich_event(event, config, account)
        if enriched is event:
            continue
        if event_exists(root, enriched):
            continue
        ingest = {"skipped": True, "dry_run": args.dry_run}
        if not args.dry_run:
            ingest = ingest_event(root, config, enriched)
        results.append(
            {
                "source_id": enriched["source_id"],
                "source_version": enriched["source_version"],
                "event_id": enriched["event_id"],
                "candidate_count": len(enriched.get("knowledge_candidates", [])),
                "ingest": ingest,
            }
        )
        if args.limit and len(results) >= args.limit:
            break
    response: dict[str, Any] = {"events_enriched": len(results), "results": results}
    if args.evolve and not args.dry_run:
        kb_cli = resolve_path(root, config.get("kb_cli")) or Path(__file__).with_name("kb.py")
        evolved = subprocess.run(
            ["python3", str(kb_cli), "evolve", str(root)],
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
        response["evolve"] = {"returncode": evolved.returncode, "stdout": evolved.stdout, "stderr": evolved.stderr}
    return response


def parser() -> argparse.ArgumentParser:
    parsed = argparse.ArgumentParser(description="Extract Gmail knowledge candidates from Raw Events")
    sub = parsed.add_subparsers(dest="command", required=True)
    backfill_command = sub.add_parser("backfill", help="Create enriched Gmail Raw Event versions with candidates")
    backfill_command.add_argument("--kb-root", type=Path, required=True)
    backfill_command.add_argument("--config", type=Path, required=True)
    backfill_command.add_argument("--limit", type=int, default=0)
    backfill_command.add_argument("--dry-run", action="store_true")
    backfill_command.add_argument("--evolve", action="store_true")
    return parsed


def main() -> int:
    try:
        args = parser().parse_args()
        if args.command == "backfill":
            print(json.dumps(backfill(args), ensure_ascii=False, indent=2))
            return 0
        raise GmailCandidateError(f"Unknown command: {args.command}")
    except GmailCandidateError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
