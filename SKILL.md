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

Read [protocol.md](references/protocol.md) for command contracts and repository layout. Read [mutation-policy.md](references/mutation-policy.md) before applying a proposal. Read [harness-adapters.md](references/harness-adapters.md) only when installing or adapting this skill to another harness.

## Execute the workflow

1. Initialize an empty repository with `init` only when the user asks to create a knowledge repository.
2. Convert hydrated source material to a Normalized Raw Event and call `ingest`. Never write crawled content directly to Wiki.
3. Call `query` with an explicit principal. Return the answer package and preserve its `trace_id`.
4. After a meaningful task, call `outcome` with the trace, claims, decisions, artifacts, and suggested updates.
5. Let `outcome` classify every suggested update as `auto_commit`, `draft`, `review_required`, or `blocked`.
6. Use `promote` only after the policy permits it. Create drafts and audit records; never silently overwrite an established Wiki page.
7. Run `lint` after mutations and before committing changes.

## Preserve evidence and permissions

- Require `tenant_id`, source identity, source version, ACL, hydration metadata, and evidence boundaries for every Raw Event.
- Treat Raw Events as append-only. Represent deletion and permission changes as new events.
- Require a principal for every query and filter Raw evidence before scoring it.
- Keep claims linked to source IDs or Wiki nodes. Mark unsupported conclusions as gaps.
- Never expand access through derived content. A Wiki node may be as restrictive as its sources, never less restrictive.
- Never move content between personal and work repositories automatically.

## Apply mutation rules

- Auto-commit only deterministic metadata operations such as adding a source, link, tag, usage count, or stale marker.
- Create drafts for evidence-backed new nodes and low-risk expansions.
- Require review for fact rewrites, conflict resolution, access changes, deletion, and durable policy or judgment changes.
- Block proposals with no evidence, unknown permission scope, or cross-repository leakage risk.
- On approval, write a new draft or an approved proposal record. Do not overwrite an existing Wiki page from the CLI.

## Respond to the user

Report which repository was used, which evidence supported the answer, what gaps were found, and which proposals were created. Distinguish clearly between a proposal, an approved draft, and committed long-term knowledge.

## Validate changes

Run:

```bash
python3 <skill-dir>/scripts/kb.py lint <kb-root>
python3 <skill-dir>/scripts/test_kb.py
```

Do not claim that semantic embeddings, remote connectors, Git synchronization, or a background service exist unless those components have been separately configured.
