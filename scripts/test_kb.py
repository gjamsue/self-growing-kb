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
    ingest_event,
    init_kb,
    lint_kb,
    list_proposals,
    promote_proposal,
    proposal_decision,
    query_kb,
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

    def test_query_filters_raw_acl_and_writes_trace(self) -> None:
        ingest_event(self.root, event())
        allowed = query_kb(self.root, "launch scope", "alice", 8, True)
        denied = query_kb(self.root, "launch scope", "bob", 8, True)
        self.assertEqual(len(allowed["evidence"]), 1)
        self.assertEqual(denied["evidence"], [])
        self.assertTrue((self.root / ".kb" / "traces" / f"{allowed['trace_id']}.json").exists())

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
