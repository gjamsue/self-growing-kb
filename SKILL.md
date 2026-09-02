---
name: self-growing-kb
description: Maintain portable, evidence-backed, self-growing Markdown knowledge bases. Use when an agent needs to initialize a knowledge vault; ingest hydrated enterprise or personal sources; query Wiki and Raw evidence with principal-aware access; record task outcomes; propose, review, or promote knowledge updates; lint knowledge integrity; or carry the same knowledge workflow across Codex, Claude Code, Gemini CLI, OpenCode, and other command-capable agent harnesses.
---

# Self-Growing Knowledge Base

Treat the knowledge repository as durable data and this skill as the portable behavior layer. Keep personal and work knowledge in separate repositories and never search across them implicitly.

## Install the skill

Clone `https://github.com/gjamsue/self-growing-kb.git` into the skill or extension directory supported by the current harness. Keep the repository intact so `SKILL.md`, `scripts/`, `references/`, and `assets/` remain adjacent.

## Locate the knowledge root

Use the root explicitly supplied by the user. Otherwise search upward from the working directory for `kb.json`. Stop and ask for a root when multiple repositories are plausible.

Run the portable CLI with:

```bash
python3 <skill-dir>/scripts/kb.py <command> <kb-root> [options]
```

Read [protocol.md](references/protocol.md) for command contracts and repository layout. Read [mutation-policy.md](references/mutation-policy.md) before applying a proposal. Read [deployment-topology.md](references/deployment-topology.md) when deciding personal-vs-work deployment, ChatGPT MCP integration, local scheduling, or multi-agent use. Read [chatgpt-mcp.md](references/chatgpt-mcp.md) when exposing a read-only Wiki MCP endpoint to ChatGPT. Read [harness-adapters.md](references/harness-adapters.md) only when installing or adapting this skill to another harness.

## Execute the workflow

1. Initialize an empty repository with `init` only when the user asks to create a knowledge repository.
2. After migrating a repository that already contains Wiki Markdown, run `bootstrap` once. If it also has legacy `01_Raw/*.md` files, run `bootstrap-raw --principal <owner>` to register them as immutable Raw Events. Both operations are replay-safe.
3. Convert hydrated source material to a Normalized Raw Event and call `ingest`. Include typed `knowledge_candidates` when hydration can extract them. Never write crawled content directly to Wiki.
4. Run `evolve` at a checkpoint, after an idle batch, or after connector synchronization. It processes only pending event deltas and emits idempotent proposals.
5. Call `query` with an explicit principal. By default it searches only active page revisions and each source's latest Raw Event. Use `--as-of` or `--include-history` only for temporal or audit questions.
6. After a meaningful task, call `outcome` with the trace and any correction, successful-pattern, stale, conflict, or missing-knowledge signals.
7. Let compile and outcome processing classify updates as `auto_commit`, `draft`, `review_required`, or `blocked`.
8. Use `promote` only after the policy permits it. Existing-page updates require complete `replacement_markdown` and the expected current revision. Approval creates a new immutable revision and switches the current pointer.
9. Use `rollback` to restore prior content as a new audited revision. Run `lint` after mutations and before committing changes.

## Preserve evidence and permissions

- Require `tenant_id`, source identity, source version, ACL, hydration metadata, and evidence boundaries for every Raw Event.
- Treat Raw Events as append-only. Represent deletion and permission changes as new events.
- Preserve source-version chains and content fingerprints. Connector cursors belong to the connector; the core consumes only normalized deltas.
- Require a principal for every query and filter Raw evidence before scoring it.
- Keep claims linked to source IDs or Wiki nodes. Mark unsupported conclusions as gaps.
- Never expand access through derived content. A Wiki node may be as restrictive as its sources, never less restrictive.
- Never move content between personal and work repositories automatically.
- Do not ingest every conversation turn. Batch at meaningful checkpoints and replay safely through deterministic identifiers.

## Apply mutation rules

- Auto-commit only deterministic metadata operations such as adding a source, link, tag, usage count, or stale marker.
- Create drafts for evidence-backed new nodes and low-risk expansions.
- Require review for fact rewrites, conflict resolution, access changes, deletion, and durable policy or judgment changes.
- Block proposals with no evidence, unknown permission scope, or cross-repository leakage risk.
- Never edit a registered Wiki page outside Promote or Rollback. Lint treats content that differs from the current revision as drift.
- Require optimistic revision matching so an older proposal cannot overwrite a newer approved revision.
- Reject replacement Markdown whose page ID differs or whose visibility exceeds the supporting permission scope.

## Respond to the user

Report which repository and revision were used, which evidence supported the answer, what gaps were found, and which proposals were created. Distinguish clearly between a proposal and an activated long-term revision.

## Validate changes

Run:

```bash
python3 <skill-dir>/scripts/kb.py lint <kb-root>
python3 <skill-dir>/scripts/test_kb.py
```

Read [self-evolution-design.md](references/self-evolution-design.md) when changing incremental ingestion, memory classes, deduplication, or learning behavior.

Do not claim that semantic embeddings, remote connectors, Git synchronization, ChatGPT MCP access, or a background service exist unless those components have been separately configured.
