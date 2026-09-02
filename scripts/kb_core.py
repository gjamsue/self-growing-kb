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
    ".kb/pages",
    ".kb/revisions",
)

SCHEMA_VERSION = 3

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
    revision_id: str | None = None
    valid_from: str | None = None


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
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


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
    if current not in {1, 2}:
        raise KBError(f"No migration path from schema {current}")
    config["schema_version"] = SCHEMA_VERSION
    config["migrated_at"] = utc_now()
    write_json(root / "kb.json", config)
    return {"status": "migrated", "from": current, "to": SCHEMA_VERSION}


def frontmatter_value(markdown: str, key: str) -> str | None:
    frontmatter = re.match(r"^---\n(.*?)\n---", markdown, flags=re.DOTALL)
    if not frontmatter:
        return None
    match = re.search(rf"^{re.escape(key)}:\s*([^\n]+)$", frontmatter.group(1), flags=re.MULTILINE)
    return match.group(1).strip().strip('"\'') if match else None


def normalize_timestamp(value: str | None, fallback: str | None = None) -> str:
    raw = value or fallback or utc_now()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return f"{raw}T00:00:00Z"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise KBError(f"Invalid timestamp: {raw}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def page_registry_path(root: Path, page_id: str) -> Path:
    validate_page_id(page_id)
    return root / ".kb" / "pages" / f"{page_id}.json"


def revision_path(root: Path, page_id: str, revision_id: str) -> Path:
    validate_page_id(page_id)
    if not re.fullmatch(r"(?:rev|rollback)_[a-z0-9]+", revision_id):
        raise KBError(f"Invalid revision_id: {revision_id}")
    return root / ".kb" / "revisions" / page_id / f"{revision_id}.json"


def validate_page_id(page_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", page_id):
        raise KBError(f"Invalid page_id: {page_id}")


def load_page_registry(root: Path, page_id: str) -> dict[str, Any] | None:
    path = page_registry_path(root, page_id)
    return read_json(path) if path.exists() else None


def revision_for_time(registry: dict[str, Any], as_of: str | None) -> str | None:
    if as_of is None:
        return registry.get("current_revision_id")
    point = normalize_timestamp(as_of)
    matches = []
    for item in registry.get("history", []):
        valid_from = normalize_timestamp(item.get("valid_from"))
        valid_to = item.get("valid_to")
        if valid_from <= point and (not valid_to or point < normalize_timestamp(valid_to)):
            matches.append((valid_from, str(item.get("activated_at", "")), str(item["revision_id"])))
    return max(matches)[2] if matches else None


def create_revision_record(
    root: Path,
    page_id: str,
    markdown: str,
    *,
    proposal_id: str,
    evidence: list[str],
    valid_from: str,
    supersedes: str | None,
    reviewer: str,
    restored_from: str | None = None,
) -> dict[str, Any]:
    content_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    revision_id = f"rev_{stable_hash({'page_id': page_id, 'content_hash': content_hash, 'proposal_id': proposal_id})}"
    record = {
        "revision_id": revision_id,
        "page_id": page_id,
        "content_hash": content_hash,
        "markdown": markdown,
        "known_at": utc_now(),
        "valid_from": normalize_timestamp(valid_from),
        "supersedes": supersedes,
        "proposal_id": proposal_id,
        "reviewer": reviewer,
        "evidence": evidence,
        "restored_from": restored_from,
    }
    target = revision_path(root, page_id, revision_id)
    if target.exists():
        existing = read_json(target)
        for key in ("revision_id", "page_id", "content_hash", "markdown", "proposal_id"):
            if existing.get(key) != record.get(key):
                raise KBError(f"Revision collision: {revision_id}")
        return existing
    write_json(target, record)
    return record


def bootstrap_pages(root: Path) -> dict[str, Any]:
    config = load_config(root)
    if int(config["schema_version"]) != SCHEMA_VERSION:
        raise KBError("Repository must be migrated before bootstrap")
    created = []
    unchanged = []
    drifted = []
    wiki_root = root / "02_Wiki"
    for path in sorted(wiki_root.rglob("*.md")):
        if "_Drafts" in path.parts:
            continue
        markdown = path.read_text(encoding="utf-8")
        page_id = frontmatter_value(markdown, "id") or path.stem
        validate_page_id(page_id)
        registry = load_page_registry(root, page_id)
        if registry:
            current = read_json(revision_path(root, page_id, registry["current_revision_id"]))
            if current["content_hash"] == hashlib.sha256(markdown.encode("utf-8")).hexdigest():
                unchanged.append(page_id)
            else:
                drifted.append(page_id)
            continue
        valid_from = normalize_timestamp(
            frontmatter_value(markdown, "updated"), frontmatter_value(markdown, "created")
        )
        revision = create_revision_record(
            root,
            page_id,
            markdown,
            proposal_id="bootstrap",
            evidence=re.findall(r"\[\[([^\]]+)\]\]", markdown),
            valid_from=valid_from,
            supersedes=None,
            reviewer="bootstrap",
        )
        registry = {
            "page_id": page_id,
            "path": str(path.relative_to(root)),
            "status": "active",
            "current_revision_id": revision["revision_id"],
            "topic_keys": [page_id],
            "history": [
                {
                    "revision_id": revision["revision_id"],
                    "status": "active",
                    "valid_from": valid_from,
                    "valid_to": None,
                    "activated_at": revision["known_at"],
                }
            ],
            "updated_at": revision["known_at"],
        }
        write_json(page_registry_path(root, page_id), registry)
        created.append(page_id)
    return {"status": "bootstrapped", "created": created, "unchanged": unchanged, "drifted": drifted}


def bootstrap_raw_sources(root: Path, principal: str) -> dict[str, Any]:
    config = load_config(root)
    if int(config["schema_version"]) != SCHEMA_VERSION:
        raise KBError("Repository must be migrated before Raw bootstrap")
    if not principal.strip():
        raise KBError("principal is required")
    accepted = []
    duplicates = []
    for path in sorted((root / "01_Raw").glob("*.md")):
        markdown = path.read_text(encoding="utf-8")
        relative_path = str(path.relative_to(root))
        source_id = frontmatter_value(markdown, "id") or path.stem
        content_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        event = {
            "event_id": f"legacy_{stable_hash({'tenant': config['tenant_id'], 'path': relative_path, 'hash': content_hash})}",
            "event_type": "created",
            "tenant_id": config["tenant_id"],
            "source_type": "legacy_markdown",
            "source_id": source_id,
            "source_version": f"bootstrap-{content_hash[:16]}",
            "source_url": relative_path,
            "title": extract_title(markdown, source_id),
            "body": markdown,
            "acl": [{"principal_type": "user", "principal_id": principal, "permission": "read"}],
            "hydration": {"method": "legacy_markdown_bootstrap", "quality": "original"},
            "evidence_boundary": {
                "proves": ["The legacy Markdown source contained this content at bootstrap."],
                "does_not_prove": ["Every claim in the source is currently true."],
            },
            "knowledge_candidates": [],
        }
        result = ingest_event(root, event)
        destination = accepted if result["status"] == "accepted" else duplicates
        destination.append(source_id)
    return {"status": "bootstrapped", "accepted": accepted, "duplicates": duplicates}


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
    hydration = dict(event.get("hydration") or {})
    hydration.pop("hydrated_at", None)
    content = {
        key: event.get(key)
        for key in (
            "event_type",
            "effective_at",
            "title",
            "body",
            "content_blocks",
            "attachments",
            "acl",
            "evidence_boundary",
            "knowledge_candidates",
        )
    }
    content["hydration"] = hydration
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
        if event_content_hash(existing) != record["content_hash"]:
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
            "content_unchanged": event_content_hash(previous_record) == record["content_hash"],
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


def iter_documents(
    root: Path,
    principal: str,
    include_raw: bool,
    profile: str,
    as_of: str | None = None,
    include_history: bool = False,
) -> Iterable[SearchDocument]:
    for registry_path in sorted((root / ".kb" / "pages").glob("*.json")):
        registry = read_json(registry_path)
        if registry.get("status") != "active" and not include_history:
            continue
        revision_ids = (
            [str(item["revision_id"]) for item in registry.get("history", [])]
            if include_history
            else [revision_for_time(registry, as_of)]
        )
        for revision_id in revision_ids:
            if not revision_id:
                continue
            revision = read_json(revision_path(root, registry["page_id"], revision_id))
            text = str(revision["markdown"])
            if not wiki_principal_can_read(text, principal, profile):
                continue
            identifier = registry["page_id"] if not include_history else f"{registry['page_id']}@{revision_id}"
            yield SearchDocument(
                kind="wiki",
                identifier=identifier,
                title=extract_title(text, registry["page_id"]),
                text=text,
                path=str(registry["path"]),
                sources=list(revision.get("evidence", [])),
                revision_id=revision_id,
                valid_from=revision.get("valid_from"),
            )
    if include_raw:
        if include_history:
            event_keys = [path.stem for path in sorted((root / "01_Raw" / "events").glob("*.json"))]
        elif as_of:
            point = normalize_timestamp(as_of)
            latest_by_source: dict[str, tuple[str, int, str]] = {}
            for path in sorted((root / "01_Raw" / "events").glob("*.json")):
                event = read_json(path)
                effective_at = normalize_timestamp(event.get("effective_at"), event.get("ingested_at"))
                if effective_at > point:
                    continue
                source_key = f"{event.get('source_type')}:{event.get('source_id')}"
                candidate = (effective_at, int(event.get("ingest_sequence", 0)), path.stem)
                if source_key not in latest_by_source or candidate > latest_by_source[source_key]:
                    latest_by_source[source_key] = candidate
            event_keys = [item[2] for item in latest_by_source.values()]
        else:
            sources = load_index(root, "sources", {"sources": {}})
            event_keys = [
                str(item["latest_event_key"])
                for item in sources.get("sources", {}).values()
                if item.get("latest_event_key")
            ]
        for key in sorted(set(event_keys)):
            path = root / "01_Raw" / "events" / f"{key}.json"
            event = read_json(path)
            if event.get("event_type") == "deleted" and not include_history:
                continue
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
                valid_from=event.get("effective_at") or event.get("ingested_at"),
            )


def query_kb(
    root: Path,
    question: str,
    principal: str,
    limit: int,
    include_raw: bool,
    as_of: str | None = None,
    include_history: bool = False,
) -> dict[str, Any]:
    config = load_config(root)
    if not principal.strip():
        raise KBError("principal is required")
    scored: list[SearchDocument] = []
    for document in iter_documents(
        root, principal, include_raw, config["profile"], as_of=as_of, include_history=include_history
    ):
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
        "as_of": normalize_timestamp(as_of) if as_of else None,
        "include_history": include_history,
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
        "as_of": trace["as_of"],
        "include_history": include_history,
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
                "revision_id": item.revision_id,
                "valid_from": item.valid_from,
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
    if update_type in {"rewrite_fact", "expand_node", "update_node"}:
        if not update.get("page_id"):
            return "blocked", "Existing-page updates require page_id."
        if not update.get("expected_revision_id"):
            return "blocked", "Existing-page updates require expected_revision_id."
        if not update.get("replacement_markdown"):
            return "blocked", "Existing-page updates require complete replacement_markdown."
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
    page_id = str(candidate.get("page_id") or slugify(str(candidate["title"])))
    base = {
        "title": candidate["title"],
        "summary": candidate["summary"],
        "evidence": evidence,
        "permission_scope": candidate_scope(event, str(candidate["permission_scope"])),
        "topic_key": candidate["topic_key"],
        "memory_type": memory_type,
        "confidence": candidate.get("confidence", "unknown"),
        "page_id": page_id,
        "claim_key": str(candidate.get("claim_key") or candidate["topic_key"]),
        "valid_from": normalize_timestamp(candidate.get("valid_from"), event.get("effective_at")),
    }
    if candidate.get("replacement_markdown"):
        base["replacement_markdown"] = str(candidate["replacement_markdown"])
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
                registry = load_page_registry(root, update["page_id"])
                update["expected_revision_id"] = registry.get("current_revision_id") if registry else None
                if previous is None and registry and update["type"] == "new_node":
                    update["type"] = "expand_node"
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
                    "page_id": update["page_id"],
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
    return slug[:80] or f"page-{stable_hash(value, 12)}"


def generated_page_markdown(page_id: str, update: dict[str, Any]) -> str:
    title = str(update.get("title") or page_id)
    scope = str(update.get("permission_scope") or "unknown")
    visibility = "public" if scope == "public" else "restricted"
    allowed = "[]" if visibility == "public" else f"[{scope}]"
    evidence = update.get("evidence", [])
    return (
        "---\n"
        f"id: {page_id}\n"
        "type: wiki\n"
        "status: active\n"
        f"visibility: {visibility}\n"
        f"allowed_principals: {allowed}\n"
        f"created: {utc_now()[:10]}\n"
        f"updated: {utc_now()[:10]}\n"
        "---\n\n"
        f"# {title}\n\n"
        f"{update.get('summary', '')}\n\n"
        "## Evidence\n\n"
        + "\n".join(f"- {item}" for item in evidence)
        + "\n"
    )


def validate_replacement_markdown(
    root: Path, page_id: str, markdown: str, permission_scope: str
) -> None:
    declared_id = frontmatter_value(markdown, "id")
    if declared_id != page_id:
        raise KBError("replacement_markdown frontmatter id must match page_id")
    if frontmatter_value(markdown, "status") not in {None, "active"}:
        raise KBError("replacement_markdown status must be active")
    if load_config(root)["profile"] != "work":
        return
    visibility = frontmatter_value(markdown, "visibility")
    if permission_scope != "public" and visibility == "public":
        raise KBError("replacement_markdown cannot widen restricted source visibility")
    if permission_scope != "public":
        allowed_raw = frontmatter_value(markdown, "allowed_principals") or ""
        allowed = {
            item.strip().strip('"\'')
            for item in allowed_raw.strip("[]").split(",")
            if item.strip()
        }
        if not allowed or not allowed.issubset({permission_scope}):
            raise KBError("replacement_markdown allowed_principals exceed permission_scope")


def activate_page_revision(
    root: Path,
    proposal: dict[str, Any],
    reviewer: str,
    restored_from: str | None = None,
) -> dict[str, Any]:
    update = proposal.get("update", {})
    page_id = str(update.get("page_id") or slugify(str(update.get("title") or "Untitled")))
    registry = load_page_registry(root, page_id)
    actual_revision = registry.get("current_revision_id") if registry else None
    expected_revision = update.get("expected_revision_id")
    if actual_revision != expected_revision:
        raise KBError(
            f"Stale proposal for {page_id}: expected {expected_revision}, current is {actual_revision}"
        )
    markdown = update.get("replacement_markdown")
    if not markdown:
        if registry:
            raise KBError("Updating an existing page requires replacement_markdown")
        markdown = generated_page_markdown(page_id, update)
    markdown = str(markdown)
    if not re.search(r"^#\s+.+$", markdown, flags=re.MULTILINE):
        raise KBError("replacement_markdown must contain an H1 heading")
    validate_replacement_markdown(root, page_id, markdown, str(update.get("permission_scope")))
    valid_from = normalize_timestamp(update.get("valid_from"))
    if registry:
        current_history = next(
            item for item in registry["history"] if item["revision_id"] == actual_revision
        )
        if valid_from < normalize_timestamp(current_history["valid_from"]):
            raise KBError("Backdated replacement requires explicit temporal reconciliation")
    revision = create_revision_record(
        root,
        page_id,
        markdown,
        proposal_id=str(proposal["proposal_id"]),
        evidence=list(update.get("evidence", [])),
        valid_from=valid_from,
        supersedes=actual_revision,
        reviewer=reviewer,
        restored_from=restored_from,
    )
    if registry:
        history = list(registry["history"])
        for item in history:
            if item["revision_id"] == actual_revision:
                item["status"] = "superseded"
                item["valid_to"] = revision["valid_from"]
                item["superseded_by"] = revision["revision_id"]
        page_path = root / registry["path"]
    else:
        history = []
        page_path = root / "02_Wiki" / f"{page_id}.md"
        registry = {
            "page_id": page_id,
            "path": str(page_path.relative_to(root)),
            "topic_keys": [],
        }
    history.append(
        {
            "revision_id": revision["revision_id"],
            "status": "active",
            "valid_from": revision["valid_from"],
            "valid_to": None,
            "activated_at": revision["known_at"],
        }
    )
    topic_key = update.get("topic_key")
    topic_keys = set(registry.get("topic_keys", []))
    if topic_key:
        topic_keys.add(str(topic_key))
    registry.update(
        {
            "status": "active",
            "current_revision_id": revision["revision_id"],
            "topic_keys": sorted(topic_keys),
            "history": history,
            "updated_at": revision["known_at"],
        }
    )
    write_text(page_path, markdown)
    write_json(page_registry_path(root, page_id), registry)
    return {
        "page_id": page_id,
        "page_path": str(page_path.relative_to(root)),
        "revision_id": revision["revision_id"],
        "superseded_revision_id": actual_revision,
    }


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

    materialized = None
    update = proposal.get("update", {})
    content_types = DRAFT_TYPES | {"rewrite_fact"}
    if action == "approve" and update.get("type") in content_types:
        materialized = activate_page_revision(root, proposal, reviewer)
    if action == "approve" and update.get("type") == "delete" and update.get("page_id"):
        registry = load_page_registry(root, str(update["page_id"]))
        if registry:
            registry["status"] = "inactive"
            registry["updated_at"] = utc_now()
            write_json(page_registry_path(root, registry["page_id"]), registry)

    proposal["status"] = "approved" if action == "approve" else "rejected"
    proposal["reviewed_at"] = utc_now()
    proposal["reviewer"] = reviewer
    if materialized:
        proposal["materialized"] = materialized
    destination_dir = root / "04_Promote" / ("approved" if action == "approve" else "rejected")
    write_json(destination_dir / path.name, proposal)
    path.unlink()
    return {
        "proposal_id": proposal_id,
        "status": proposal["status"],
        "audit_path": str((destination_dir / path.name).relative_to(root)),
        **(materialized or {}),
    }


def rollback_page(root: Path, page_id: str, revision_id: str, reviewer: str) -> dict[str, Any]:
    registry = load_page_registry(root, page_id)
    if not registry:
        raise KBError(f"Unknown page_id: {page_id}")
    target = read_json(revision_path(root, page_id, revision_id))
    target_markdown = str(target["markdown"])
    target_scope = "public" if frontmatter_value(target_markdown, "visibility") == "public" else None
    if target_scope is None:
        allowed_raw = frontmatter_value(target_markdown, "allowed_principals") or ""
        target_scope = next(
            (item.strip().strip('"\'') for item in allowed_raw.strip("[]").split(",") if item.strip()),
            "rollback",
        )
    proposal_id = f"rollback_{stable_hash({'page_id': page_id, 'target': revision_id, 'current': registry['current_revision_id']})}"
    proposal = {
        "proposal_id": proposal_id,
        "update": {
            "type": "update_node",
            "page_id": page_id,
            "title": extract_title(str(target["markdown"]), page_id),
            "replacement_markdown": target_markdown,
            "evidence": target.get("evidence", []),
            "permission_scope": target_scope,
            "valid_from": utc_now(),
            "expected_revision_id": registry["current_revision_id"],
        },
    }
    result = activate_page_revision(root, proposal, reviewer, restored_from=revision_id)
    audit = {**proposal, "status": "approved", "reviewer": reviewer, "reviewed_at": utc_now(), "materialized": result}
    write_json(root / "04_Promote" / "approved" / f"{proposal_id}.json", audit)
    return {"status": "rolled_back", **result, "restored_from": revision_id}


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
    page_registries = [read_json(path) for path in (root / ".kb" / "pages").glob("*.json")]
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
        "pages": {
            "total": len(page_registries),
            "active": sum(1 for page in page_registries if page.get("status") == "active"),
            "inactive": sum(1 for page in page_registries if page.get("status") != "active"),
        },
        "revisions": len(list((root / ".kb" / "revisions").glob("*/*.json"))),
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

    page_count = 0
    revision_count = 0
    registered_paths: set[str] = set()
    for path in sorted((root / ".kb" / "pages").glob("*.json")):
        page_count += 1
        try:
            registry = read_json(path)
            page_id = str(registry["page_id"])
            if path.stem != page_id:
                errors.append(f"Page registry filename mismatch: {path.relative_to(root)}")
            registered_paths.add(str(registry["path"]))
            current_id = str(registry["current_revision_id"])
            current = read_json(revision_path(root, page_id, current_id))
            page_path = root / str(registry["path"])
            if not page_path.exists():
                errors.append(f"Registered Wiki page is missing: {registry['path']}")
            elif hashlib.sha256(page_path.read_bytes()).hexdigest() != current.get("content_hash"):
                errors.append(f"Wiki page drifted from current revision: {registry['path']}")
            active = [item for item in registry.get("history", []) if item.get("status") == "active"]
            if len(active) != 1 or active[0].get("revision_id") != current_id:
                errors.append(f"Page registry has an invalid current pointer: {page_id}")
            for item in registry.get("history", []):
                revision_count += 1
                read_json(revision_path(root, page_id, str(item["revision_id"])))
        except (KBError, KeyError) as exc:
            errors.append(str(exc))
    for path in wiki_paths:
        relative = str(path.relative_to(root))
        if "_Drafts" not in path.parts and relative not in registered_paths:
            errors.append(f"Active Wiki page is not registered: {relative}")

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
            "registered_pages": page_count,
            "revisions": revision_count,
        },
    }
