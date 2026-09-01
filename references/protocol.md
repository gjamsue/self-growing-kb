# Portable Knowledge Base Protocol

## Repository layout

```text
kb.json
00_Index/
01_Raw/events/
02_Wiki/_Drafts/
03_Query/
04_Promote/proposals/
04_Promote/approved/
04_Promote/rejected/
05_Lint/
.kb/traces/
.kb/outcomes/
.kb/compile-queue/{pending,processed,failed}/
.kb/indexes/
.kb/gaps/
```

`kb.json` binds one repository to one `profile` and `tenant_id`. Never change either value after ingesting data. Create another repository instead.

## Commands

### Initialize

```bash
python3 scripts/kb.py init /path/to/wiki --profile personal --tenant-id personal-jian
```

Initialization is idempotent when the requested profile and tenant match the existing config.

### Ingest

```bash
python3 scripts/kb.py ingest /path/to/wiki raw-event.json
```

The event identity is `tenant_id + source_type + source_id + source_version`. Replaying identical content returns `duplicate`; changing content under the same identity fails.

Ingest stores a normalized content hash, links the prior source version, records changed fields, updates the source index, and writes one durable compile job. `knowledge_candidates` are optional typed outputs from hydration; the core does not pretend to perform semantic extraction by itself.

### Migrate

```bash
python3 scripts/kb.py migrate /path/to/wiki
```

Migration upgrades repository structure and `kb.json` without rewriting Raw Events or Wiki pages.

### Compile and evolve

```bash
python3 scripts/kb.py compile /path/to/wiki
python3 scripts/kb.py evolve /path/to/wiki
python3 scripts/kb.py status /path/to/wiki
```

Compile consumes pending jobs once. It deduplicates candidates by `topic_key`: facts and instructions that changed create supersession reviews, events accumulate, and ephemeral tasks are skipped. `evolve` runs compile and returns status plus lint. Neither command approves proposals or overwrites established Wiki.

### Query

```bash
python3 scripts/kb.py query /path/to/wiki "What did we decide?" --principal user-123
```

The local search implementation is a deterministic lexical baseline. It searches Markdown Wiki nodes and ACL-permitted Raw Events, writes a trace, increments document usage, and records repeated no-result questions as knowledge gaps. A future vector or graph retriever may replace ranking but must preserve the output contract and permission filter.

### Submit outcome

```bash
python3 scripts/kb.py outcome /path/to/wiki outcome.json
```

The referenced trace must exist. Suggested updates and typed learning signals become deterministic proposals, so replay does not duplicate mutations.

### Review proposals

```bash
python3 scripts/kb.py proposals /path/to/wiki --status pending
python3 scripts/kb.py promote /path/to/wiki prop_123 --action approve --reviewer user-123
```

Approved new or expanded content becomes a file in `02_Wiki/_Drafts/`. Established Wiki pages are never overwritten by the CLI.

### Lint

```bash
python3 scripts/kb.py lint /path/to/wiki
```

Lint checks structure, JSON validity, tenant consistency, Raw Event identity, Wiki headings, proposal records, and unresolved links.

## Harness contract

Every harness adapter must preserve these rules:

1. Pass the knowledge root explicitly.
2. Pass the human or service principal explicitly.
3. Preserve `trace_id` through task completion.
4. Submit outcomes only for meaningful work, not every chat turn.
5. Submit connector deltas using the connector's native cursor or change token; do not rescan unchanged history.
6. Treat returned excerpts as evidence candidates, not guaranteed truth.
7. Never move data between profiles automatically.
8. Run lint after approved mutations.

## Exit behavior

Successful CLI calls print JSON and exit `0`. Contract or validation failures print a JSON error to stderr and exit `2`.
