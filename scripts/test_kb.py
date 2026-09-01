#!/usr/bin/env python3
"""Unit and workflow tests for self-growing-kb."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kb_core import (
    KBError,
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
    }


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

    def test_migrate_v1_repository_without_rewriting_data(self) -> None:
        config_path = self.root / "kb.json"
        config = read_json(config_path)
        config["schema_version"] = 1
        config_path.write_text(json.dumps(config), encoding="utf-8")
        result = migrate_kb(self.root)
        self.assertEqual(result, {"status": "migrated", "from": 1, "to": 2})
        self.assertEqual(read_json(config_path)["schema_version"], 2)
        self.assertEqual(migrate_kb(self.root)["status"], "already_current")

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
        alice = query_kb(self.root, "launch method", "alice", 8, False)
        bob = query_kb(self.root, "launch method", "bob", 8, False)
        self.assertEqual([item["id"] for item in alice["evidence"]], ["visible"])
        self.assertEqual(bob["evidence"], [])

    def test_outcome_classifies_and_promotes_draft_without_overwrite(self) -> None:
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
                }
            ],
        }
        submitted = submit_outcome(self.root, outcome)
        proposal_id = submitted["update_proposals"][0]["proposal_id"]
        self.assertEqual(submitted["update_proposals"][0]["decision"], "draft")
        promoted = promote_proposal(self.root, proposal_id, "approve", "alice")
        self.assertEqual(promoted["status"], "approved")
        self.assertTrue((self.root / promoted["draft_path"]).exists())
        self.assertEqual(len(list_proposals(self.root, "approved")), 1)

    def test_compile_deduplicates_topics_and_requires_review_for_supersession(self) -> None:
        first_event = event()
        first_event["knowledge_candidates"] = [candidate()]
        ingest_event(self.root, first_event)
        first_compile = compile_pending(self.root)
        first_proposal = first_compile["jobs"][0]["proposals"][0]
        self.assertEqual(first_proposal["decision"], "draft")

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
        self.assertEqual(decisions, ["draft", "review_required"])

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
        self.assertEqual(first["update_proposals"][0]["decision"], "review_required")

    def test_evolve_reports_status_and_lint(self) -> None:
        source_event = event()
        source_event["knowledge_candidates"] = [candidate()]
        ingest_event(self.root, source_event)
        result = evolve_kb(self.root)
        self.assertEqual(result["compiled"]["count"], 1)
        self.assertEqual(result["status"]["compile_queue"]["pending"], 0)
        self.assertTrue(result["lint"]["ok"], result["lint"])

    def test_mutation_policy_blocks_leakage_and_requires_review(self) -> None:
        blocked, _ = proposal_decision(
            {"type": "new_node", "evidence": ["evt_1"], "permission_scope": "alice", "cross_repository": True}
        )
        review, _ = proposal_decision(
            {"type": "rewrite_fact", "evidence": ["evt_1"], "permission_scope": "alice"}
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
        lint = lint_kb(self.root)
        self.assertTrue(lint["ok"], lint)
        self.assertEqual(lint["warnings"], [])
        cli = Path(__file__).with_name("kb.py")
        result = subprocess.run([sys.executable, str(cli), "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Portable self-growing knowledge base CLI", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
