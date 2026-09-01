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

### Query

```bash
python3 scripts/kb.py query /path/to/wiki "What did we decide?" --principal user-123
```

The local search implementation is a deterministic lexical baseline. It searches Markdown Wiki nodes and ACL-permitted Raw Events, then writes a trace under `.kb/traces/`. A future vector or graph retriever may replace ranking but must preserve the output contract and permission filter.

### Submit outcome

```bash
python3 scripts/kb.py outcome /path/to/wiki outcome.json
```

The referenced trace must exist. Each suggested update becomes one immutable proposal with a mutation decision.

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
5. Treat returned excerpts as evidence candidates, not guaranteed truth.
6. Never move data between profiles automatically.
7. Run lint after approved mutations.

## Exit behavior

Successful CLI calls print JSON and exit `0`. Contract or validation failures print a JSON error to stderr and exit `2`.
