#!/usr/bin/env python3
"""Unit and workflow tests for self-growing-kb."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from email.message import EmailMessage
from pathlib import Path

from gmail_api_sync import decode_base64url, normalize_thread
from gmail_imap_sync import imap_search_args, normalize_imap_message
from gmail_thread_to_event import build_event
from kb_core import (
    KBError,
    bootstrap_pages,
    bootstrap_raw_sources,
    compile_pending,
    evolve_kb,
    ingest_event,
    init_kb,
    lint_kb,
    list_proposals,
    migrate_kb,
    promote_proposal,
    proposal_decision,
    query_kb,
    read_json,
    rollback_page,
    status_kb,
    submit_outcome,
)


def event(tenant: str = "tenant-test") -> dict:
    return {
        "event_id": "evt_001",
        "event_type": "created",
        "tenant_id": tenant,
        "source_type": "slack",
        "source_id": "message-001",
        "source_version": "1",
        "title": "Launch scope decision",
        "body": "The launch scope includes search and evidence citations.",
        "acl": [{"principal_type": "user", "principal_id": "alice", "permission": "read"}],
        "hydration": {"method": "api", "quality": "original"},
        "evidence_boundary": {
            "proves": ["The message contained the launch scope statement."],
            "does_not_prove": ["The launch was completed."],
        },
    }


def candidate(memory_type: str = "fact", summary: str = "Search requires evidence citations.") -> dict:
    return {
        "memory_type": memory_type,
        "topic_key": "launch/search-evidence",
        "title": "Search evidence rule",
        "summary": summary,
        "permission_scope": "alice",
        "confidence": "high",
        "page_id": "search-evidence-rule",
        "replacement_markdown": page_markdown(
            "Search evidence rule", summary, "search-evidence-rule"
        ),
    }


def page_markdown(title: str, body: str, page_id: str = "launch-scope-method") -> str:
    return (
        "---\n"
        f"id: {page_id}\n"
        "type: wiki\n"
        "status: active\n"
        "visibility: restricted\n"
        "allowed_principals: [alice]\n"
        "created: 2026-08-31\n"
        "updated: 2026-09-01\n"
        "---\n\n"
        f"# {title}\n\n{body}\n"
    )


def gmail_thread(message_id: str = "msg-1", thread_id: str = "thread-1") -> dict:
    return {
        "id": thread_id,
        "history_id": "123",
        "messages": [
            {
                "id": message_id,
                "thread_id": thread_id,
                "label_ids": ["INBOX", "CATEGORY_PERSONAL"],
                "history_id": "122",
                "internal_date": "1788310315000",
                "snippet": "Snippet fallback",
                "payload": {
                    "mime_type": "multipart/alternative",
                    "headers": [
                        {"name": "From", "value": "Teacher <teacher@example.com>"},
                        {"name": "To", "value": "Parent <parent@example.com>"},
                        {"name": "Subject", "value": "School transition"},
                        {"name": "Date", "value": "Tue, 1 Sep 2026 17:51:55 -0700"},
                    ],
                    "parts": [
                        {
                            "mime_type": "text/plain",
                            "body": {"content": "Alina understands the requested phrases."},
                        }
                    ],
                },
            }
        ],
    }


def gmail_rest_thread() -> dict:
    encoded = "QVBJIGJvZHkgdGV4dA"
    return {
        "id": "api-thread-1",
        "historyId": "456",
        "messages": [
            {
                "id": "api-msg-1",
                "threadId": "api-thread-1",
                "labelIds": ["INBOX"],
                "historyId": "455",
                "internalDate": "1788310315000",
                "snippet": "Snippet text",
                "payload": {
                    "mimeType": "multipart/alternative",
                    "headers": [
                        {"name": "From", "value": "Sender <sender@example.com>"},
                        {"name": "To", "value": "Reader <reader@example.com>"},
                        {"name": "Subject", "value": "API thread"},
                    ],
                    "parts": [
                        {
                            "partId": "0",
                            "mimeType": "text/plain",
                            "body": {"size": 13, "data": encoded},
                        }
                    ],
                },
            }
        ],
    }


def imap_email() -> bytes:
    message = EmailMessage()
    message["From"] = "Sender <sender@example.com>"
    message["To"] = "Reader <reader@example.com>"
    message["Subject"] = "IMAP thread"
    message["Date"] = "Tue, 1 Sep 2026 17:51:55 -0700"
    message["Message-ID"] = "<imap-msg-1@example.com>"
    message.set_content("IMAP body text\n")
    return bytes(message)


class KnowledgeBaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "wiki"
        init_kb(self.root, "work", "tenant-test")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_init_is_idempotent_and_rejects_profile_change(self) -> None:
        result = init_kb(self.root, "work", "tenant-test")
        self.assertEqual(result["status"], "already_initialized")
        with self.assertRaises(KBError):
            init_kb(self.root, "personal", "tenant-test")

    def test_ingest_is_idempotent_and_detects_collision(self) -> None:
        first = ingest_event(self.root, event())
        second = ingest_event(self.root, event())
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "duplicate")
        changed = event()
        changed["body"] = "Changed under the same source version"
        with self.assertRaises(KBError):
            ingest_event(self.root, changed)

    def test_ingest_ignores_hydration_time_for_idempotency(self) -> None:
        original = event()
        original["hydration"]["hydrated_at"] = "2026-09-02T00:00:00Z"
        repeat = event()
        repeat["hydration"]["hydrated_at"] = "2026-09-02T01:00:00Z"
        first = ingest_event(self.root, original)
        second = ingest_event(self.root, repeat)
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "duplicate")

    def test_gmail_thread_adapter_uses_mailbox_identity_for_multiple_accounts(self) -> None:
        base_args = {
            "tenant_id": "tenant-test",
            "principal": "alice",
            "event_type": "updated",
            "version_mode": "latest-message-id",
            "source_url": None,
            "hydrated_at": "2026-09-02T00:00:00Z",
        }
        personal = build_event(Namespace(**base_args, mailbox_account="personal@example.com"), gmail_thread())
        work = build_event(Namespace(**base_args, mailbox_account="work@example.com"), gmail_thread())

        self.assertEqual(personal["source_type"], "mail")
        self.assertEqual(personal["source_version"], "message:msg-1")
        self.assertIn("gmail/personal@example.com/thread/thread-1", personal["source_id"])
        self.assertIn("gmail/work@example.com/thread/thread-1", work["source_id"])
        self.assertNotEqual(personal["source_id"], work["source_id"])
        self.assertEqual(personal["mailbox"]["account"], "personal@example.com")
        self.assertIn("Alina understands the requested phrases.", personal["body"])

    def test_gmail_api_thread_normalization_decodes_rest_payload(self) -> None:
        self.assertEqual(decode_base64url("QVBJIGJvZHkgdGV4dA"), "API body text")
        plain = normalize_thread(gmail_rest_thread(), "plain")
        event = build_event(
            Namespace(
                tenant_id="tenant-test",
                principal="alice",
                event_type="updated",
                version_mode="thread-history-id",
                source_url=None,
                hydrated_at="2026-09-02T00:00:00Z",
                mailbox_account="api@example.com",
            ),
            plain,
        )
        self.assertEqual(event["source_id"], "gmail/api@example.com/thread/api-thread-1")
        self.assertEqual(event["source_version"], "history:456")
        self.assertIn("API body text", event["body"])

        snippet = normalize_thread(gmail_rest_thread(), "snippet")
        snippet_event = build_event(
            Namespace(
                tenant_id="tenant-test",
                principal="alice",
                event_type="updated",
                version_mode="latest-message-id",
                source_url=None,
                hydrated_at="2026-09-02T00:00:00Z",
                mailbox_account="api@example.com",
            ),
            snippet,
        )
        self.assertIn("Snippet text", snippet_event["body"])
        self.assertNotIn("API body text", snippet_event["body"])

    def test_gmail_imap_message_normalization_and_search_args(self) -> None:
        self.assertEqual(
            imap_search_args({"gmail_search": "newer_than:7d -in:spam"}, {}),
            ["X-GM-RAW", '"newer_than:7d -in:spam"'],
        )
        self.assertEqual(
            imap_search_args({}, {"imap_search": "SINCE 01-Sep-2026 NOT DELETED"}),
            ["SINCE", "01-Sep-2026", "NOT", "DELETED"],
        )

        normalized = normalize_imap_message(
            imap_email(),
            {"uid": "42", "x_gm_thrid": "thread-42", "x_gm_msgid": "msg-42"},
            "plain",
        )
        event = build_event(
            Namespace(
                tenant_id="tenant-test",
                principal="alice",
                event_type="updated",
                version_mode="latest-message-id",
                source_url=None,
                hydrated_at="2026-09-02T00:00:00Z",
                mailbox_account="imap@example.com",
            ),
            {"id": "thread-42", "history_id": "42", "messages": [normalized]},
        )
        self.assertEqual(event["source_id"], "gmail/imap@example.com/thread/thread-42")
        self.assertEqual(event["source_version"], "message:msg-42")
        self.assertIn("IMAP body text", event["body"])

    def test_migrate_v1_repository_without_rewriting_data(self) -> None:
        config_path = self.root / "kb.json"
        config = read_json(config_path)
        config["schema_version"] = 1
        config_path.write_text(json.dumps(config), encoding="utf-8")
        result = migrate_kb(self.root)
        self.assertEqual(result, {"status": "migrated", "from": 1, "to": 3})
        self.assertEqual(read_json(config_path)["schema_version"], 3)
        self.assertEqual(migrate_kb(self.root)["status"], "already_current")

    def test_migrate_v2_repository_to_v3(self) -> None:
        config_path = self.root / "kb.json"
        config = read_json(config_path)
        config["schema_version"] = 2
        config_path.write_text(json.dumps(config), encoding="utf-8")
        result = migrate_kb(self.root)
        self.assertEqual(result, {"status": "migrated", "from": 2, "to": 3})
        self.assertTrue((self.root / ".kb/pages").is_dir())
        self.assertTrue((self.root / ".kb/revisions").is_dir())

    def test_ingest_builds_version_chain_and_persistent_queue(self) -> None:
        first_event = event()
        first = ingest_event(self.root, first_event)
        second_event = event()
        second_event.update({"event_id": "evt_002", "source_version": "2", "body": "Updated scope"})
        second = ingest_event(self.root, second_event)
        record = read_json(self.root / "01_Raw" / "events" / f"{second['idempotency_key']}.json")
        self.assertEqual(record["version_chain"]["previous_event_key"], first["idempotency_key"])
        self.assertEqual(record["version_chain"]["changed_fields"], ["body"])
        self.assertEqual(len(list((self.root / ".kb/compile-queue/pending").glob("*.json"))), 2)

    def test_query_filters_raw_acl_and_writes_trace(self) -> None:
        ingest_event(self.root, event())
        allowed = query_kb(self.root, "launch scope", "alice", 8, True)
        denied = query_kb(self.root, "launch scope", "bob", 8, True)
        self.assertEqual(len(allowed["evidence"]), 1)
        self.assertEqual(denied["evidence"], [])
        self.assertTrue((self.root / ".kb" / "traces" / f"{allowed['trace_id']}.json").exists())
        status = status_kb(self.root)
        self.assertEqual(status["queries"], 2)
        self.assertEqual(status["knowledge_gaps"], 1)

    def test_work_wiki_is_default_deny(self) -> None:
        restricted = self.root / "02_Wiki" / "restricted.md"
        restricted.write_text("# Secret launch method\n", encoding="utf-8")
        visible = self.root / "02_Wiki" / "visible.md"
        visible.write_text(
            "---\nvisibility: restricted\nallowed_principals: [alice]\n---\n\n# Visible launch method\n",
            encoding="utf-8",
        )
        bootstrap_pages(self.root)
        alice = query_kb(self.root, "launch method", "alice", 8, False)
        bob = query_kb(self.root, "launch method", "bob", 8, False)
        self.assertEqual([item["id"] for item in alice["evidence"]], ["visible"])
        self.assertEqual(bob["evidence"], [])

    def test_outcome_promotes_new_node_to_active_revision(self) -> None:
        ingest_event(self.root, event())
        query = query_kb(self.root, "launch scope", "alice", 8, True)
        outcome = {
            "trace_id": query["trace_id"],
            "agent_id": "agent-1",
            "task_id": "task-1",
            "tenant_id": "tenant-test",
            "principal": "alice",
            "outcome_summary": "A reusable launch scope concept was identified.",
            "suggested_updates": [
                {
                    "type": "new_node",
                    "title": "Launch scope method",
                    "summary": "Define launch scope with evidence citations.",
                    "evidence": ["evt_001"],
                    "permission_scope": "alice",
                    "page_id": "launch-scope-method",
                    "valid_from": "2026-09-01",
                }
            ],
        }
        submitted = submit_outcome(self.root, outcome)
        proposal_id = submitted["update_proposals"][0]["proposal_id"]
        self.assertEqual(submitted["update_proposals"][0]["decision"], "draft")
        promoted = promote_proposal(self.root, proposal_id, "approve", "alice")
        self.assertEqual(promoted["status"], "approved")
        self.assertTrue((self.root / promoted["page_path"]).exists())
        registry = read_json(self.root / ".kb/pages/launch-scope-method.json")
        self.assertEqual(registry["current_revision_id"], promoted["revision_id"])
        self.assertEqual(len(list_proposals(self.root, "approved")), 1)

    def test_compile_deduplicates_topics_and_requires_review_for_supersession(self) -> None:
        first_event = event()
        first_event["knowledge_candidates"] = [candidate()]
        ingest_event(self.root, first_event)
        first_compile = compile_pending(self.root)
        first_proposal = first_compile["jobs"][0]["proposals"][0]
        self.assertEqual(first_proposal["decision"], "draft")
        promote_proposal(self.root, first_proposal["proposal_id"], "approve", "alice")

        second_event = event()
        second_event.update({"event_id": "evt_002", "source_version": "2"})
        second_event["knowledge_candidates"] = [candidate(summary="Search requires citations and ACL checks.")]
        ingest_event(self.root, second_event)
        second_compile = compile_pending(self.root)
        second_proposal = second_compile["jobs"][0]["proposals"][0]
        self.assertEqual(second_proposal["decision"], "review_required")
        proposal = next(item for item in list_proposals(self.root) if item["proposal_id"] == second_proposal["proposal_id"])
        self.assertEqual(proposal["update"]["type"], "rewrite_fact")
        self.assertEqual(compile_pending(self.root)["count"], 0)

    def test_compile_respects_ingest_order_when_multiple_versions_are_pending(self) -> None:
        first_event = event()
        first_event["knowledge_candidates"] = [candidate(summary="Version one")]
        ingest_event(self.root, first_event)
        second_event = event()
        second_event.update({"event_id": "evt_002", "source_version": "2"})
        second_event["knowledge_candidates"] = [candidate(summary="Version two")]
        ingest_event(self.root, second_event)
        result = compile_pending(self.root)
        decisions = [job["proposals"][0]["decision"] for job in result["jobs"]]
        self.assertEqual(decisions, ["draft", "blocked"])

    def test_compile_blocks_candidate_scope_outside_source_acl(self) -> None:
        source_event = event()
        source_event["knowledge_candidates"] = [candidate() | {"permission_scope": "bob"}]
        ingest_event(self.root, source_event)
        result = compile_pending(self.root)
        self.assertEqual(result["jobs"][0]["proposals"][0]["decision"], "blocked")

    def test_permission_and_delete_events_require_review(self) -> None:
        for version, event_type in (("1", "permission_changed"), ("2", "deleted")):
            source_event = event()
            source_event.update({"event_id": f"evt_{version}", "source_version": version, "event_type": event_type})
            ingest_event(self.root, source_event)
        result = compile_pending(self.root)
        proposals = [job["proposals"][0] for job in result["jobs"]]
        self.assertEqual([item["decision"] for item in proposals], ["review_required", "review_required"])

    def test_compile_skips_ephemeral_tasks(self) -> None:
        task_event = event()
        task_event["knowledge_candidates"] = [candidate(memory_type="task")]
        ingest_event(self.root, task_event)
        result = compile_pending(self.root)
        self.assertEqual(result["jobs"][0]["proposals"], [])
        self.assertEqual(result["jobs"][0]["skipped"][0]["reason"], "ephemeral_task")

    def test_learning_signals_are_idempotent(self) -> None:
        ingest_event(self.root, event())
        query = query_kb(self.root, "launch scope", "alice", 8, True)
        outcome = {
            "trace_id": query["trace_id"],
            "agent_id": "agent-1",
            "task_id": "task-2",
            "tenant_id": "tenant-test",
            "principal": "alice",
            "outcome_summary": "The prior claim needs correction.",
            "learning_signals": [
                {
                    "signal_type": "correction",
                    "title": "Launch scope",
                    "summary": "Correct the scope.",
                    "evidence": ["evt_001"],
                    "permission_scope": "alice",
                }
            ],
        }
        first = submit_outcome(self.root, outcome)
        second = submit_outcome(self.root, outcome)
        self.assertEqual(first["outcome_id"], second["outcome_id"])
        self.assertEqual(first["update_proposals"], second["update_proposals"])
        self.assertEqual(first["update_proposals"][0]["decision"], "blocked")

    def test_evolve_reports_status_and_lint(self) -> None:
        source_event = event()
        source_event["knowledge_candidates"] = [candidate()]
        ingest_event(self.root, source_event)
        result = evolve_kb(self.root)
        self.assertEqual(result["compiled"]["count"], 1)
        self.assertEqual(result["status"]["compile_queue"]["pending"], 0)
        self.assertTrue(result["lint"]["ok"], result["lint"])

    def test_bootstrap_registers_active_pages_and_detects_drift(self) -> None:
        page = self.root / "02_Wiki/existing.md"
        page.write_text(page_markdown("Existing", "Original content", "existing"), encoding="utf-8")
        draft = self.root / "02_Wiki/_Drafts/draft.md"
        draft.write_text("# Draft content\n", encoding="utf-8")
        first = bootstrap_pages(self.root)
        self.assertEqual(first["created"], ["existing"])
        self.assertTrue(lint_kb(self.root)["ok"])
        page.write_text(page_markdown("Existing", "Untracked edit", "existing"), encoding="utf-8")
        second = bootstrap_pages(self.root)
        self.assertEqual(second["drifted"], ["existing"])
        self.assertFalse(lint_kb(self.root)["ok"])

    def test_bootstrap_legacy_raw_markdown_is_idempotent(self) -> None:
        source = self.root / "01_Raw/source-legacy.md"
        source.write_text("---\nid: source-legacy\n---\n\n# Legacy evidence\n\nOriginal body.\n", encoding="utf-8")
        first = bootstrap_raw_sources(self.root, "alice")
        second = bootstrap_raw_sources(self.root, "alice")
        self.assertEqual(first["accepted"], ["source-legacy"])
        self.assertEqual(second["duplicates"], ["source-legacy"])
        self.assertEqual(evolve_kb(self.root)["compiled"]["count"], 1)
        result = query_kb(self.root, "Original body", "alice", 8, True)
        self.assertEqual(result["evidence"][0]["kind"], "raw")

    def test_query_uses_latest_raw_version_and_hides_deleted_source(self) -> None:
        first = event()
        first["body"] = "YesterdayLegacyOnly launch"
        first["effective_at"] = "2026-08-31T00:00:00Z"
        ingest_event(self.root, first)
        second = event()
        second.update({
            "event_id": "evt_002",
            "source_version": "2",
            "body": "TodayCurrentOnly launch",
            "effective_at": "2026-09-01T00:00:00Z",
        })
        ingest_event(self.root, second)
        self.assertEqual(query_kb(self.root, "YesterdayLegacyOnly", "alice", 8, True)["evidence"], [])
        current = query_kb(self.root, "TodayCurrentOnly", "alice", 8, True)
        self.assertEqual([item["id"] for item in current["evidence"]], ["evt_002"])
        historical = query_kb(
            self.root, "YesterdayLegacyOnly", "alice", 8, True, as_of="2026-08-31T12:00:00Z"
        )
        self.assertEqual([item["id"] for item in historical["evidence"]], ["evt_001"])
        deleted = event()
        deleted.update({
            "event_id": "evt_003",
            "source_version": "3",
            "event_type": "deleted",
            "effective_at": "2026-09-02T00:00:00Z",
        })
        ingest_event(self.root, deleted)
        self.assertEqual(query_kb(self.root, "TodayCurrentOnly", "alice", 8, True)["evidence"], [])

    def test_replacement_switches_current_and_preserves_as_of_history(self) -> None:
        ingest_event(self.root, event())
        trace = query_kb(self.root, "launch", "alice", 8, True)["trace_id"]
        first_outcome = {
            "trace_id": trace,
            "agent_id": "agent-1",
            "task_id": "create-page",
            "tenant_id": "tenant-test",
            "principal": "alice",
            "outcome_summary": "Create the initial page.",
            "suggested_updates": [{
                "type": "new_node",
                "page_id": "launch-scope-method",
                "title": "Launch scope method",
                "summary": "YesterdayOnly",
                "replacement_markdown": page_markdown("Launch scope method", "YesterdayOnly"),
                "valid_from": "2026-08-31",
                "evidence": ["evt_001"],
                "permission_scope": "alice",
            }],
        }
        first_proposal = submit_outcome(self.root, first_outcome)["update_proposals"][0]["proposal_id"]
        first_revision = promote_proposal(self.root, first_proposal, "approve", "alice")["revision_id"]
        trace2 = query_kb(self.root, "YesterdayOnly", "alice", 8, False)["trace_id"]
        second_outcome = {
            "trace_id": trace2,
            "agent_id": "agent-1",
            "task_id": "replace-page",
            "tenant_id": "tenant-test",
            "principal": "alice",
            "outcome_summary": "Replace the page with current evidence.",
            "suggested_updates": [{
                "type": "rewrite_fact",
                "page_id": "launch-scope-method",
                "title": "Launch scope method",
                "summary": "TodayOnly",
                "replacement_markdown": page_markdown("Launch scope method", "TodayOnly"),
                "valid_from": "2026-09-01",
                "expected_revision_id": first_revision,
                "evidence": ["evt_001"],
                "permission_scope": "alice",
            }],
        }
        second_proposal = submit_outcome(self.root, second_outcome)["update_proposals"][0]["proposal_id"]
        second = promote_proposal(self.root, second_proposal, "approve", "alice")
        self.assertEqual(second["superseded_revision_id"], first_revision)
        self.assertEqual(query_kb(self.root, "YesterdayOnly", "alice", 8, False)["evidence"], [])
        historical = query_kb(self.root, "YesterdayOnly", "alice", 8, False, as_of="2026-08-31T12:00:00Z")
        self.assertEqual(historical["as_of"], "2026-08-31T12:00:00Z")
        self.assertEqual(historical["evidence"][0]["revision_id"], first_revision)
        self.assertTrue(query_kb(self.root, "TodayOnly", "alice", 8, False)["evidence"])

        rolled_back = rollback_page(self.root, "launch-scope-method", first_revision, "alice")
        self.assertEqual(rolled_back["status"], "rolled_back")
        self.assertTrue(query_kb(self.root, "YesterdayOnly", "alice", 8, False)["evidence"])

    def test_stale_proposal_cannot_overwrite_newer_revision(self) -> None:
        page = self.root / "02_Wiki/existing.md"
        page.write_text(page_markdown("Existing", "Version one", "existing"), encoding="utf-8")
        bootstrap_pages(self.root)
        registry = read_json(self.root / ".kb/pages/existing.json")
        current = registry["current_revision_id"]
        ingest_event(self.root, event())
        trace = query_kb(self.root, "launch", "alice", 8, True)["trace_id"]
        base = {
            "trace_id": trace,
            "agent_id": "agent-1",
            "tenant_id": "tenant-test",
            "principal": "alice",
            "outcome_summary": "Update existing.",
        }
        def submit(task: str, body: str) -> str:
            payload = dict(base)
            payload["task_id"] = task
            payload["suggested_updates"] = [{
                "type": "update_node",
                "page_id": "existing",
                "title": "Existing",
                "summary": body,
                "replacement_markdown": page_markdown("Existing", body, "existing"),
                "expected_revision_id": current,
                "valid_from": "2026-09-01",
                "evidence": ["evt_001"],
                "permission_scope": "alice",
            }]
            return submit_outcome(self.root, payload)["update_proposals"][0]["proposal_id"]
        first = submit("update-a", "Version two")
        stale = submit("update-b", "Conflicting version")
        promote_proposal(self.root, first, "approve", "alice")
        with self.assertRaises(KBError):
            promote_proposal(self.root, stale, "approve", "alice")

    def test_bootstrap_rejects_unsafe_page_id(self) -> None:
        page = self.root / "02_Wiki/unsafe.md"
        page.write_text("---\nid: ../unsafe\n---\n\n# Unsafe\n", encoding="utf-8")
        with self.assertRaises(KBError):
            bootstrap_pages(self.root)

    def test_replacement_cannot_widen_source_acl(self) -> None:
        page = self.root / "02_Wiki/existing.md"
        page.write_text(page_markdown("Existing", "Private", "existing"), encoding="utf-8")
        bootstrap_pages(self.root)
        current = read_json(self.root / ".kb/pages/existing.json")["current_revision_id"]
        ingest_event(self.root, event())
        trace = query_kb(self.root, "launch", "alice", 8, True)["trace_id"]
        public_replacement = page_markdown("Existing", "Leaked", "existing").replace(
            "visibility: restricted", "visibility: public"
        )
        outcome = {
            "trace_id": trace,
            "agent_id": "agent-1",
            "task_id": "widen",
            "tenant_id": "tenant-test",
            "principal": "alice",
            "outcome_summary": "Unsafe visibility change.",
            "suggested_updates": [{
                "type": "update_node",
                "page_id": "existing",
                "title": "Existing",
                "summary": "Leaked",
                "replacement_markdown": public_replacement,
                "expected_revision_id": current,
                "evidence": ["evt_001"],
                "permission_scope": "alice",
            }],
        }
        proposal_id = submit_outcome(self.root, outcome)["update_proposals"][0]["proposal_id"]
        with self.assertRaises(KBError):
            promote_proposal(self.root, proposal_id, "approve", "alice")

    def test_mutation_policy_blocks_leakage_and_requires_review(self) -> None:
        blocked, _ = proposal_decision(
            {"type": "new_node", "evidence": ["evt_1"], "permission_scope": "alice", "cross_repository": True}
        )
        review, _ = proposal_decision(
            {
                "type": "rewrite_fact",
                "page_id": "existing",
                "expected_revision_id": "rev_123",
                "replacement_markdown": page_markdown("Existing", "Replacement", "existing"),
                "evidence": ["evt_1"],
                "permission_scope": "alice",
            }
        )
        self.assertEqual(blocked, "blocked")
        self.assertEqual(review, "review_required")

    def test_lint_and_cli_help(self) -> None:
        ingest_event(self.root, event())
        (self.root / "00_Index" / "linked-index.md").write_text("# Linked index\n", encoding="utf-8")
        (self.root / "02_Wiki" / "linked.md").write_text(
            "---\nvisibility: public\n---\n\n# Linked\n\n[[linked-index]] and `[[not-a-real-link]]`\n",
            encoding="utf-8",
        )
        bootstrap_pages(self.root)
        lint = lint_kb(self.root)
        self.assertTrue(lint["ok"], lint)
        self.assertEqual(lint["warnings"], [])
        cli = Path(__file__).with_name("kb.py")
        result = subprocess.run([sys.executable, str(cli), "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Portable self-growing knowledge base CLI", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
