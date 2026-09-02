# Self-Evolution Design

## Reference workflow

The Xiaohongshu video "基于Codex+Obsidian的自生长知识库2.0" by 智见洞察 demonstrates a useful human-visible loop: preserve Raw source material, compile reusable Wiki pages, browse relationships in Obsidian, query across pages with an agent, selectively promote durable results, and lint integrity. The implementation here keeps that loop but adds the machinery required for enterprise and multi-harness operation.

Canonical reference: <https://www.xiaohongshu.com/discovery/item/6a890a81000000002800aa4e>

## Evidence-backed extensions

- Incremental sources should emit deltas rather than replay full history. Microsoft Graph delta query is a concrete change-token model: <https://learn.microsoft.com/en-us/graph/api/message-delta?view=graph-rest-1.0>
- Unchanged content should be skipped using stable document hashes. LlamaIndex documents this doc-id and hash strategy: <https://docs.llamaindex.ai/en/v0.10.17/module_guides/loading/ingestion_pipeline/root.html>
- Memory ingestion benefits from extraction, classification, deduplication, and type-specific behavior. Cloudflare distinguishes superseding facts/instructions, accumulating events, and ephemeral tasks: <https://developers.cloudflare.com/agent-memory/concepts/how-agent-memory-works/>
- Conversation memory should be batched at checkpoints and use a cursor for unseen messages rather than ingesting every turn: <https://developers.cloudflare.com/agent-memory/get-started/>
- Every update needs provenance and version history. AWS describes immutable memory versions and audit trails: <https://docs.aws.amazon.com/devopsagent/latest/userguide/about-aws-devops-agent-devops-agent-memories.html>
- Useful operational learning includes symptoms, successful steps, root causes, and pitfalls linked to source sessions. Azure documents this post-session learning model: <https://learn.microsoft.com/en-us/azure/sre-agent/memory>

## Implemented loop

```text
connector cursor
  -> hydrated Raw Event
  -> append-only evidence + content hash + source version chain
  -> persistent compile queue
  -> typed candidate classification
  -> topic-key deduplication
  -> metadata commit, draft, review, or block
  -> principal-aware query + usage/gap trace
  -> agent outcome learning signals
  -> idempotent proposal
  -> optimistic revision check
  -> immutable revision + current-pointer switch
  -> lint and Git audit trail
```

The connector owns polling, webhooks, native cursors, hydration, and semantic candidate extraction. The core owns deterministic storage, deduplication, policy, traces, and proposals. This boundary keeps the portable skill dependency-free and prevents it from claiming semantic capabilities it does not provide.

Registered page revisions are the retrieval authority. Markdown under `02_Wiki` is the Obsidian-readable materialized current view. Normal retrieval excludes drafts, superseded page revisions, prior Raw versions, and deleted sources. Historical retrieval is explicit through `as_of` or `include_history`.

The ledger records both `known_at` (when the repository accepted a revision) and `valid_from` (when its content is intended to be effective). The current implementation supports ordered replacement and point-in-time reads; backdated replacement that would rewrite an existing validity interval is blocked for explicit reconciliation.

## Safety boundary

Self-evolution means the system can discover and propose better durable knowledge from new evidence and observed use. It does not mean self-authorizing truth. Facts, instructions, ACLs, deletion, conflicts, and cross-repository movement remain review governed.
