#!/usr/bin/env python3
"""Dependency-free core for a portable, proposal-first Markdown knowledge base."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


KB_DIRS = (
    "00_Index",
    "01_Raw/events",
    "02_Wiki/_Drafts",
    "03_Query",
    "04_Promote/proposals",
    "04_Promote/approved",
    "04_Promote/rejected",
    "05_Lint",
    ".kb/traces",
    ".kb/outcomes",
    ".kb/compile-queue/pending",
    ".kb/compile-queue/processed",
    ".kb/compile-queue/failed",
    ".kb/indexes",
    ".kb/gaps",
)

SCHEMA_VERSION = 2

REQUIRED_EVENT_FIELDS = (
    "event_id",
    "event_type",
    "tenant_id",
    "source_type",
    "source_id",
    "source_version",
    "title",
    "body",
    "acl",
    "hydration",
    "evidence_boundary",
)

HIGH_RISK_TYPES = {
    "rewrite_fact",
    "resolve_conflict",
    "widen_access",
    "change_acl",
    "delete",
    "long_term_judgment",
    "policy_change",
}

AUTO_TYPES = {"add_source", "add_link", "add_tag", "increment_usage", "mark_stale"}
DRAFT_TYPES = {"new_node", "expand_node", "update_node"}
MEMORY_TYPES = {"fact", "event", "instruction", "task"}


class KBError(ValueError):
    pass


@dataclass(frozen=True)
class SearchDocument:
    kind: str
    identifier: str
    title: str
    text: str
    path: str
    sources: list[str]
    score: float = 0.0


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KBError(f"File not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise KBError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise KBError(f"Expected a JSON object in {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def stable_hash(value: Any, length: int = 24) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:length]


def load_index(root: Path, name: str, default: dict[str, Any]) -> dict[str, Any]:
    path = root / ".kb" / "indexes" / f"{name}.json"
    return read_json(path) if path.exists() else dict(default)


def save_index(root: Path, name: str, value: dict[str, Any]) -> None:
    write_json(root / ".kb" / "indexes" / f"{name}.json", value)


def load_config(root: Path) -> dict[str, Any]:
    config = read_json(root / "kb.json")
    for key in ("schema_version", "profile", "tenant_id"):
        if not config.get(key):
            raise KBError(f"kb.json is missing {key}")
    return config


def init_kb(root: Path, profile: str, tenant_id: str) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "kb.json"
    if config_path.exists():
        existing = load_config(root)
        if existing["profile"] != profile or existing["tenant_id"] != tenant_id:
            raise KBError("Existing kb.json has a different profile or tenant_id")
        return {"status": "already_initialized", "root": str(root), "config": existing}

    for relative in KB_DIRS:
        (root / relative).mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": SCHEMA_VERSION,
        "profile": profile,
        "tenant_id": tenant_id,
        "created_at": utc_now(),
        "query": {"include_raw_by_default": True, "default_limit": 8},
    }
    write_json(config_path, config)
    (root / "00_Index" / "start-here.md").write_text(
        "# Start Here\n\nThis vault is managed by the self-growing-kb skill.\n",
        encoding="utf-8",
    )
    return {"status": "initialized", "root": str(root), "config": config}


def migrate_kb(root: Path) -> dict[str, Any]:
    config = load_config(root)
    current = int(config["schema_version"])
    if current > SCHEMA_VERSION:
        raise KBError(f"Repository schema {current} is newer than supported schema {SCHEMA_VERSION}")
    for relative in KB_DIRS:
        (root / relative).mkdir(parents=True, exist_ok=True)
    if current == SCHEMA_VERSION:
        return {"status": "already_current", "schema_version": current}
    if current != 1:
        raise KBError(f"No migration path from schema {current}")
    config["schema_version"] = SCHEMA_VERSION
    config["migrated_at"] = utc_now()
    write_json(root / "kb.json", config)
    return {"status": "migrated", "from": current, "to": SCHEMA_VERSION}


def validate_event(event: dict[str, Any], tenant_id: str) -> None:
    missing = [key for key in REQUIRED_EVENT_FIELDS if key not in event]
    if missing:
        raise KBError(f"Raw Event missing fields: {', '.join(missing)}")
    if event["tenant_id"] != tenant_id:
        raise KBError("Raw Event tenant_id does not match kb.json")
    if not isinstance(event["acl"], list) or not event["acl"]:
        raise KBError("Raw Event acl must be a non-empty list")
    if not isinstance(event["hydration"], dict) or not event["hydration"].get("quality"):
        raise KBError("Raw Event hydration.quality is required")
    boundary = event["evidence_boundary"]
    if not isinstance(boundary, dict) or "proves" not in boundary or "does_not_prove" not in boundary:
        raise KBError("Raw Event evidence_boundary must include proves and does_not_prove")
    candidates = event.get("knowledge_candidates", [])
    if not isinstance(candidates, list):
        raise KBError("Raw Event knowledge_candidates must be a list")
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise KBError("Each knowledge candidate must be an object")
        missing_candidate = [
            key for key in ("memory_type", "topic_key", "title", "summary", "permission_scope")
            if not candidate.get(key)
        ]
        if missing_candidate:
            raise KBError(f"Knowledge candidate missing fields: {', '.join(missing_candidate)}")
        if candidate["memory_type"] not in MEMORY_TYPES:
            raise KBError(f"Unknown memory_type: {candidate['memory_type']}")


def event_key(event: dict[str, Any]) -> str:
    raw = ":".join(
        str(event[key]) for key in ("tenant_id", "source_type", "source_id", "source_version")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def event_content_hash(event: dict[str, Any]) -> str:
    content = {
        key: event.get(key)
        for key in (
            "event_type",
            "title",
            "body",
            "content_blocks",
            "attachments",
            "acl",
            "hydration",
            "evidence_boundary",
            "knowledge_candidates",
        )
    }
    return stable_hash(content, 64)


def changed_event_fields(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    ignored = {
        "event_id",
        "source_version",
        "ingested_at",
        "ingest_sequence",
        "idempotency_key",
        "content_hash",
        "version_chain",
    }
    keys = set(previous) | set(current)
    return sorted(key for key in keys - ignored if previous.get(key) != current.get(key))


def find_previous_event(root: Path, event: dict[str, Any]) -> tuple[Path, dict[str, Any]] | None:
    matches: list[tuple[Path, dict[str, Any]]] = []
    for path in (root / "01_Raw" / "events").glob("*.json"):
        record = read_json(path)
        if record.get("source_type") == event["source_type"] and record.get("source_id") == event["source_id"]:
            matches.append((path, record))
    if not matches:
        return None
    matches.sort(
        key=lambda item: (
            int(item[1].get("ingest_sequence", 0)),
            str(item[1].get("ingested_at", "")),
            item[0].name,
        )
    )
    return matches[-1]


def ingest_event(root: Path, event: dict[str, Any]) -> dict[str, Any]:
    config = load_config(root)
    if int(config["schema_version"]) < SCHEMA_VERSION:
        raise KBError("Repository must be migrated before ingest: run `kb.py migrate <root>`")
    validate_event(event, config["tenant_id"])
    key = event_key(event)
    target = root / "01_Raw" / "events" / f"{key}.json"
    record = dict(event)
    record["idempotency_key"] = key
    record["content_hash"] = event_content_hash(event)
    record.setdefault("ingested_at", utc_now())
    if target.exists():
        existing = read_json(target)
        if any(existing.get(field) != value for field, value in event.items()):
            raise KBError("Idempotency collision: source version already exists with different content")
        return {"status": "duplicate", "raw_event_id": event["event_id"], "idempotency_key": key}
    state = load_index(root, "state", {"next_ingest_sequence": 1})
    record["ingest_sequence"] = int(state.get("next_ingest_sequence", 1))
    state["next_ingest_sequence"] = record["ingest_sequence"] + 1
    previous = find_previous_event(root, event)
    if previous:
        previous_path, previous_record = previous
        record["version_chain"] = {
            "previous_event_key": previous_path.stem,
            "previous_version": previous_record.get("source_version"),
            "changed_fields": changed_event_fields(previous_record, record),
            "content_unchanged": previous_record.get("content_hash") == record["content_hash"],
        }
    else:
        record["version_chain"] = {
            "previous_event_key": None,
            "previous_version": None,
            "changed_fields": [],
            "content_unchanged": False,
        }
    write_json(target, record)
    job = {
        "job_id": f"compile_{key}",
        "event_key": key,
        "tenant_id": config["tenant_id"],
        "status": "pending",
        "created_at": utc_now(),
        "content_hash": record["content_hash"],
        "ingest_sequence": record["ingest_sequence"],
    }
    write_json(root / ".kb" / "compile-queue" / "pending" / f"compile_{key}.json", job)

    sources = load_index(root, "sources", {"sources": {}})
    source_key = f"{event['source_type']}:{event['source_id']}"
    sources["sources"][source_key] = {
        "latest_event_key": key,
        "latest_version": event["source_version"],
        "content_hash": record["content_hash"],
        "updated_at": record["ingested_at"],
    }
    save_index(root, "sources", sources)
    save_index(root, "state", state)
    return {
        "status": "accepted",
        "raw_event_id": event["event_id"],
        "idempotency_key": key,
        "path": str(target.relative_to(root)),
        "content_hash": record["content_hash"],
        "previous_event_key": record["version_chain"]["previous_event_key"],
        "changed_fields": record["version_chain"]["changed_fields"],
        "queued_jobs": [job["job_id"]],
    }


def principal_can_read(acl: list[dict[str, Any]], principal: str) -> bool:
    for entry in acl:
        if entry.get("permission") != "read":
            continue
        if entry.get("principal_type") == "public":
            return True
        if entry.get("principal_id") == principal:
            return True
    return False


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9_.-]*|[\u4e00-\u9fff]", text.lower())


def score_text(question: str, title: str, text: str) -> float:
    query_tokens = set(tokenize(question))
    if not query_tokens:
        return 0.0
    title_tokens = set(tokenize(title))
    text_tokens = set(tokenize(text))
    score = 3.0 * len(query_tokens & title_tokens) + len(query_tokens & text_tokens)
    if question.lower() in f"{title}\n{text}".lower():
        score += 8.0
    return score


def extract_title(markdown: str, fallback: str) -> str:
    match = re.search(r"^#\s+(.+)$", markdown, flags=re.MULTILINE)
    return match.group(1).strip() if match else fallback


def strip_code(markdown: str) -> str:
    without_fences = re.sub(r"```.*?```", "", markdown, flags=re.DOTALL)
    return re.sub(r"`[^`]*`", "", without_fences)


def wiki_principal_can_read(markdown: str, principal: str, profile: str) -> bool:
    if profile == "personal":
        return True
    frontmatter = re.match(r"^---\n(.*?)\n---", markdown, flags=re.DOTALL)
    if not frontmatter:
        return False
    metadata = frontmatter.group(1)
    visibility = re.search(r"^visibility:\s*([^\n]+)$", metadata, flags=re.MULTILINE)
    if visibility and visibility.group(1).strip().strip('"\'') == "public":
        return True
    allowed = re.search(r"^allowed_principals:\s*\[([^\]]*)\]$", metadata, flags=re.MULTILINE)
    if not allowed:
        return False
    principals = {item.strip().strip('"\'') for item in allowed.group(1).split(",") if item.strip()}
    return principal in principals


def iter_documents(root: Path, principal: str, include_raw: bool, profile: str) -> Iterable[SearchDocument]:
    wiki_dir = root / "02_Wiki"
    if wiki_dir.exists():
        for path in sorted(wiki_dir.rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            if not wiki_principal_can_read(text, principal, profile):
                continue
            yield SearchDocument(
                kind="wiki",
                identifier=path.stem,
                title=extract_title(text, path.stem),
                text=text,
                path=str(path.relative_to(root)),
                sources=re.findall(r"\[\[([^\]]+)\]\]", text),
            )
    if include_raw:
        for path in sorted((root / "01_Raw" / "events").glob("*.json")):
            event = read_json(path)
            if not principal_can_read(event.get("acl", []), principal):
                continue
            body = str(event.get("body", ""))
            yield SearchDocument(
                kind="raw",
                identifier=str(event.get("event_id", path.stem)),
                title=str(event.get("title", path.stem)),
                text=body,
                path=str(path.relative_to(root)),
                sources=[str(event.get("source_id", ""))],
            )


def query_kb(root: Path, question: str, principal: str, limit: int, include_raw: bool) -> dict[str, Any]:
    config = load_config(root)
    if not principal.strip():
        raise KBError("principal is required")
    scored: list[SearchDocument] = []
    for document in iter_documents(root, principal, include_raw, config["profile"]):
        score = score_text(question, document.title, document.text)
        if score > 0:
            scored.append(SearchDocument(**{**document.__dict__, "score": score}))
    scored.sort(key=lambda item: (-item.score, item.kind, item.identifier))
    selected = scored[:limit]
    trace_id = f"trace_{uuid.uuid4().hex}"
    gaps = [] if selected else ["No matching evidence was found."]
    trace = {
        "trace_id": trace_id,
        "created_at": utc_now(),
        "tenant_id": config["tenant_id"],
        "principal": principal,
        "question": question,
        "results": [document.identifier for document in selected],
        "gaps": gaps,
    }
    write_json(root / ".kb" / "traces" / f"{trace_id}.json", trace)
    usage = load_index(root, "usage", {"documents": {}, "queries": 0})
    usage["queries"] = int(usage.get("queries", 0)) + 1
    for document in selected:
        usage_key = f"{document.kind}:{document.identifier}"
        item = usage["documents"].setdefault(usage_key, {"count": 0, "last_used_at": None})
        item["count"] += 1
        item["last_used_at"] = trace["created_at"]
    save_index(root, "usage", usage)
    if not selected:
        gap_key = stable_hash({"tenant_id": config["tenant_id"], "question": question.strip().lower()})
        gap_path = root / ".kb" / "gaps" / f"{gap_key}.json"
        gap = read_json(gap_path) if gap_path.exists() else {
            "gap_id": gap_key,
            "question": question,
            "tenant_id": config["tenant_id"],
            "count": 0,
            "first_seen_at": trace["created_at"],
        }
        gap["count"] += 1
        gap["last_seen_at"] = trace["created_at"]
        gap["latest_trace_id"] = trace_id
        write_json(gap_path, gap)
    return {
        "trace_id": trace_id,
        "question": question,
        "principal": principal,
        "confidence": "medium" if selected else "low",
        "evidence": [
            {
                "kind": item.kind,
                "id": item.identifier,
                "title": item.title,
                "path": item.path,
                "score": item.score,
                "excerpt": re.sub(r"\s+", " ", item.text).strip()[:320],
                "sources": item.sources,
            }
            for item in selected
        ],
        "gaps": gaps,
    }


def proposal_decision(update: dict[str, Any]) -> tuple[str, str]:
    update_type = str(update.get("type", "")).strip()
    evidence = update.get("evidence") or []
    if update.get("cross_repository"):
        return "blocked", "Cross-repository movement requires explicit, separate export and review."
    if update.get("permission_scope") in (None, "unknown"):
        return "blocked", "Permission scope is unknown."
    if not evidence:
        return "blocked", "No evidence was supplied."
    if update_type in HIGH_RISK_TYPES:
        return "review_required", "This change affects facts, access, deletion, conflict, or durable judgment."
    if update_type in AUTO_TYPES:
        return "auto_commit", "This is a deterministic metadata mutation."
    if update_type in DRAFT_TYPES:
        return "draft", "This is an evidence-backed content proposal and must remain a draft until promoted."
    return "review_required", "Unknown mutation types require review."


def create_proposal(
    root: Path,
    config: dict[str, Any],
    update: dict[str, Any],
    origin: dict[str, Any],
) -> dict[str, Any]:
    decision, reason = proposal_decision(update)
    fingerprint = stable_hash({"tenant_id": config["tenant_id"], "update": update, "origin": origin})
    proposal_id = f"prop_{fingerprint}"
    path = root / "04_Promote" / "proposals" / f"{proposal_id}.json"
    if path.exists():
        proposal = read_json(path)
        return {"proposal_id": proposal_id, "decision": proposal["decision"], "status": proposal["status"]}
    proposal = {
        "proposal_id": proposal_id,
        "fingerprint": fingerprint,
        "created_at": utc_now(),
        "status": "pending" if decision != "blocked" else "blocked",
        "decision": decision,
        "reason": reason,
        "tenant_id": config["tenant_id"],
        **origin,
        "update": update,
    }
    write_json(path, proposal)
    return {"proposal_id": proposal_id, "decision": decision, "status": proposal["status"]}


def candidate_scope(event: dict[str, Any], requested_scope: str) -> str:
    readable = [entry for entry in event.get("acl", []) if entry.get("permission") == "read"]
    if any(entry.get("principal_type") == "public" for entry in readable):
        return requested_scope
    if any(entry.get("principal_id") == requested_scope for entry in readable):
        return requested_scope
    return "unknown"


def source_permission_scope(event: dict[str, Any]) -> str:
    readable = [entry for entry in event.get("acl", []) if entry.get("permission") == "read"]
    if any(entry.get("principal_type") == "public" for entry in readable):
        return "public"
    principals = sorted(str(entry.get("principal_id")) for entry in readable if entry.get("principal_id"))
    return f"acl:{stable_hash(principals)}" if principals else "unknown"


def candidate_update(candidate: dict[str, Any], event: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any] | None:
    memory_type = candidate["memory_type"]
    evidence = list(candidate.get("evidence") or [event["event_id"]])
    base = {
        "title": candidate["title"],
        "summary": candidate["summary"],
        "evidence": evidence,
        "permission_scope": candidate_scope(event, str(candidate["permission_scope"])),
        "topic_key": candidate["topic_key"],
        "memory_type": memory_type,
        "confidence": candidate.get("confidence", "unknown"),
    }
    if memory_type == "task":
        return None
    if previous is None or memory_type == "event":
        return {"type": "new_node", **base}
    if previous.get("content_hash") == stable_hash({"title": candidate["title"], "summary": candidate["summary"]}, 64):
        return {"type": "add_source", **base}
    if memory_type in {"fact", "instruction"}:
        return {
            "type": "rewrite_fact",
            **base,
            "supersedes_proposal_id": previous.get("latest_proposal_id"),
        }
    return {"type": "expand_node", **base}


def compile_pending(root: Path, limit: int = 100) -> dict[str, Any]:
    config = load_config(root)
    if int(config["schema_version"]) < SCHEMA_VERSION:
        raise KBError("Repository must be migrated before compile")
    topics = load_index(root, "topics", {"topics": {}})
    processed_jobs: list[dict[str, Any]] = []
    queued_jobs = [
        (path, read_json(path))
        for path in (root / ".kb" / "compile-queue" / "pending").glob("*.json")
    ]
    queued_jobs.sort(key=lambda item: (int(item[1].get("ingest_sequence", 0)), item[0].name))
    for job_path, job in queued_jobs[:limit]:
        event_path = root / "01_Raw" / "events" / f"{job['event_key']}.json"
        try:
            event = read_json(event_path)
            created = []
            skipped = []
            if event.get("event_type") in {"deleted", "permission_changed"}:
                governance_type = "delete" if event["event_type"] == "deleted" else "change_acl"
                governance = {
                    "type": governance_type,
                    "title": str(event.get("title") or event["source_id"]),
                    "summary": f"Source emitted a {event['event_type']} event.",
                    "evidence": [str(event["event_id"])],
                    "permission_scope": source_permission_scope(event),
                    "source_type": event["source_type"],
                    "source_id": event["source_id"],
                }
                created.append(
                    create_proposal(
                        root,
                        config,
                        governance,
                        {"origin_type": "compile", "event_key": job["event_key"], "topic_key": "source-governance"},
                    )
                )
            for candidate in event.get("knowledge_candidates", []):
                topic_key = str(candidate["topic_key"])
                previous = topics["topics"].get(topic_key)
                update = candidate_update(candidate, event, previous)
                if update is None:
                    skipped.append({"topic_key": topic_key, "reason": "ephemeral_task"})
                    continue
                proposal = create_proposal(
                    root,
                    config,
                    update,
                    {"origin_type": "compile", "event_key": job["event_key"], "topic_key": topic_key},
                )
                created.append(proposal)
                content_proposal_id = proposal["proposal_id"]
                if update["type"] == "add_source" and previous:
                    content_proposal_id = previous.get("latest_proposal_id", content_proposal_id)
                topics["topics"][topic_key] = {
                    "memory_type": candidate["memory_type"],
                    "content_hash": stable_hash(
                        {"title": candidate["title"], "summary": candidate["summary"]}, 64
                    ),
                    "latest_proposal_id": content_proposal_id,
                    "latest_event_key": job["event_key"],
                    "updated_at": utc_now(),
                }
            job.update({"status": "processed", "processed_at": utc_now(), "proposals": created, "skipped": skipped})
            destination = root / ".kb" / "compile-queue" / "processed" / job_path.name
            write_json(destination, job)
            job_path.unlink()
            processed_jobs.append({"job_id": job["job_id"], "proposals": created, "skipped": skipped})
        except (KBError, KeyError, TypeError) as exc:
            job.update({"status": "failed", "failed_at": utc_now(), "error": str(exc)})
            write_json(root / ".kb" / "compile-queue" / "failed" / job_path.name, job)
            job_path.unlink()
            processed_jobs.append({"job_id": job.get("job_id", job_path.stem), "error": str(exc)})
    save_index(root, "topics", topics)
    return {"status": "processed", "jobs": processed_jobs, "count": len(processed_jobs)}


def updates_from_learning_signals(outcome: dict[str, Any]) -> list[dict[str, Any]]:
    mapping = {
        "correction": "rewrite_fact",
        "successful_pattern": "new_node",
        "stale_claim": "mark_stale",
        "conflict": "resolve_conflict",
        "missing_knowledge": "research_gap",
    }
    updates = []
    for signal in outcome.get("learning_signals", []):
        update = dict(signal)
        signal_type = str(update.pop("signal_type", ""))
        update["type"] = mapping.get(signal_type, signal_type)
        updates.append(update)
    return updates


def submit_outcome(root: Path, outcome: dict[str, Any]) -> dict[str, Any]:
    config = load_config(root)
    for key in ("trace_id", "agent_id", "task_id", "tenant_id", "principal", "outcome_summary"):
        if not outcome.get(key):
            raise KBError(f"Outcome missing {key}")
    if outcome["tenant_id"] != config["tenant_id"]:
        raise KBError("Outcome tenant_id does not match kb.json")
    trace_path = root / ".kb" / "traces" / f"{outcome['trace_id']}.json"
    if not trace_path.exists():
        raise KBError(f"Unknown trace_id: {outcome['trace_id']}")

    outcome_id = str(outcome.get("outcome_id") or f"outcome_{stable_hash(outcome)}")
    saved_outcome = dict(outcome)
    saved_outcome["outcome_id"] = outcome_id
    outcome_path = root / ".kb" / "outcomes" / f"{outcome_id}.json"
    if outcome_path.exists():
        saved_outcome = read_json(outcome_path)
    else:
        saved_outcome.setdefault("created_at", utc_now())
        write_json(outcome_path, saved_outcome)

    proposals = []
    all_updates = list(outcome.get("suggested_updates", [])) + updates_from_learning_signals(outcome)
    for update in all_updates:
        proposals.append(
            create_proposal(
                root,
                config,
                update,
                {
                    "origin_type": "outcome",
                    "trace_id": outcome["trace_id"],
                    "outcome_id": outcome_id,
                    "principal": outcome["principal"],
                },
            )
        )
    return {"status": "accepted", "outcome_id": outcome_id, "update_proposals": proposals}


def list_proposals(root: Path, status: str | None = None) -> list[dict[str, Any]]:
    load_config(root)
    proposals = []
    for path in sorted((root / "04_Promote").rglob("*.json")):
        proposal = read_json(path)
        if status is None or proposal.get("status") == status:
            proposals.append(proposal)
    return proposals


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or f"draft-{uuid.uuid4().hex[:8]}"


def promote_proposal(root: Path, proposal_id: str, action: str, reviewer: str) -> dict[str, Any]:
    load_config(root)
    path = root / "04_Promote" / "proposals" / f"{proposal_id}.json"
    proposal = read_json(path)
    if proposal.get("status") not in {"pending", "blocked"}:
        raise KBError(f"Proposal is already {proposal.get('status')}")
    if action not in {"approve", "reject"}:
        raise KBError("action must be approve or reject")
    if proposal.get("decision") == "blocked" and action == "approve":
        raise KBError("Blocked proposals cannot be approved; resolve the blocking condition first")

    proposal["status"] = "approved" if action == "approve" else "rejected"
    proposal["reviewed_at"] = utc_now()
    proposal["reviewer"] = reviewer
    destination_dir = root / "04_Promote" / ("approved" if action == "approve" else "rejected")
    write_json(destination_dir / path.name, proposal)

    draft_path = None
    update = proposal.get("update", {})
    if action == "approve" and update.get("type") in DRAFT_TYPES:
        title = str(update.get("title") or "Untitled knowledge draft")
        draft_path = root / "02_Wiki" / "_Drafts" / f"{slugify(title)}--{proposal_id[-8:]}.md"
        evidence = update.get("evidence", [])
        draft_path.write_text(
            "---\n"
            f"id: {slugify(title)}\n"
            "type: wiki-draft\n"
            "status: draft\n"
            "visibility: restricted\n"
            f"allowed_principals: [{proposal['principal']}]\n"
            f"proposal_id: {proposal_id}\n"
            f"created: {utc_now()}\n"
            "---\n\n"
            f"# {title}\n\n"
            f"{update.get('summary', '')}\n\n"
            "## Evidence\n\n"
            + "\n".join(f"- {item}" for item in evidence)
            + "\n",
            encoding="utf-8",
        )
    path.unlink()
    return {
        "proposal_id": proposal_id,
        "status": proposal["status"],
        "audit_path": str((destination_dir / path.name).relative_to(root)),
        "draft_path": str(draft_path.relative_to(root)) if draft_path else None,
    }


def status_kb(root: Path) -> dict[str, Any]:
    config = load_config(root)
    queue_root = root / ".kb" / "compile-queue"
    queue = {
        state: len(list((queue_root / state).glob("*.json")))
        for state in ("pending", "processed", "failed")
    }
    gaps = [read_json(path) for path in (root / ".kb" / "gaps").glob("*.json")]
    gaps.sort(key=lambda item: (-int(item.get("count", 0)), str(item.get("question", ""))))
    topics = load_index(root, "topics", {"topics": {}})
    usage = load_index(root, "usage", {"documents": {}, "queries": 0})
    proposal_status: dict[str, int] = {}
    for proposal in list_proposals(root):
        status = str(proposal.get("status", "unknown"))
        proposal_status[status] = proposal_status.get(status, 0) + 1
    return {
        "schema_version": config["schema_version"],
        "profile": config["profile"],
        "tenant_id": config["tenant_id"],
        "compile_queue": queue,
        "topics": len(topics.get("topics", {})),
        "queries": int(usage.get("queries", 0)),
        "knowledge_gaps": len(gaps),
        "top_gaps": gaps[:5],
        "proposals": proposal_status,
    }


def evolve_kb(root: Path, limit: int = 100) -> dict[str, Any]:
    compiled = compile_pending(root, limit)
    return {"compiled": compiled, "status": status_kb(root), "lint": lint_kb(root)}


def lint_kb(root: Path) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        config = load_config(root)
    except KBError as exc:
        return {"ok": False, "errors": [str(exc)], "warnings": [], "counts": {}}

    if int(config["schema_version"]) != SCHEMA_VERSION:
        errors.append(
            f"Unsupported schema_version {config['schema_version']}; run migrate to reach {SCHEMA_VERSION}"
        )

    for relative in KB_DIRS:
        if not (root / relative).exists():
            errors.append(f"Missing directory: {relative}")

    raw_count = 0
    for path in sorted((root / "01_Raw" / "events").glob("*.json")):
        raw_count += 1
        try:
            event = read_json(path)
            validate_event(event, config["tenant_id"])
            if path.stem != event_key(event):
                errors.append(f"Raw Event filename does not match idempotency key: {path}")
        except KBError as exc:
            errors.append(str(exc))

    wiki_paths = list((root / "02_Wiki").rglob("*.md"))
    all_markdown_paths = list(root.rglob("*.md"))
    known_ids = {path.stem.split("--", 1)[0] for path in all_markdown_paths}
    broken_links: set[str] = set()
    for path in wiki_paths:
        text = path.read_text(encoding="utf-8")
        if not re.search(r"^#\s+.+$", text, flags=re.MULTILINE):
            warnings.append(f"Wiki file has no H1: {path.relative_to(root)}")
        for link in re.findall(r"\[\[([^\]|#]+)", strip_code(text)):
            if link not in known_ids:
                broken_links.add(link)
    if broken_links:
        warnings.append("Unresolved wiki links: " + ", ".join(sorted(broken_links)))

    proposal_count = 0
    for path in (root / "04_Promote").rglob("*.json"):
        proposal_count += 1
        try:
            read_json(path)
        except KBError as exc:
            errors.append(str(exc))

    queue_count = 0
    for state in ("pending", "processed", "failed"):
        for path in (root / ".kb" / "compile-queue" / state).glob("*.json"):
            queue_count += 1
            try:
                job = read_json(path)
                if job.get("status") != state:
                    errors.append(f"Compile job status/path mismatch: {path.relative_to(root)}")
            except KBError as exc:
                errors.append(str(exc))

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "counts": {
            "raw_events": raw_count,
            "wiki_nodes": len(wiki_paths),
            "proposals": proposal_count,
            "compile_jobs": queue_count,
        },
    }
