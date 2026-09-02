# Harness Adapters

Keep the CLI and repository protocol authoritative. Harness adapters should only translate prompts, tool calls, and lifecycle events.

## Codex

Install the `self-growing-kb` folder as a Skill or bundle it into a Codex plugin. Invoke `scripts/kb.py` for deterministic operations. Add an MCP wrapper only when remote or long-running access is needed.

For a repository available on the local filesystem, Codex should treat the local checkout as authoritative for queries, proposals, promotions, and connector refreshes. Use OS scheduling such as `launchd`, `cron`, CI, or a long-running worker for periodic refresh; do not rely on the chat session staying open.

## Claude Code

Expose the same `SKILL.md` under the harness's supported skills directory. Translate tool names in examples to shell execution. Keep `kb.py` unchanged.

For Claude Code, keep `self-growing-kb` as the portable behavior layer and point it at the same local work wiki checkout. Do not fork the protocol for Claude-specific state; only adapt how shell commands are invoked.

## Gemini CLI

Install the instructions using the supported skill or extension mechanism. Keep the CLI JSON contract unchanged and map task completion to `outcome`.

## OpenCode and generic harnesses

Reference `SKILL.md` from the harness instruction file and allow execution of `python3 scripts/kb.py`. If the harness supports MCP, wrap the CLI operations without changing their payloads.

## Future MCP tool mapping

| MCP tool | CLI operation |
|---|---|
| `kb_init` | `init` |
| `kb_ingest` | `ingest` |
| `kb_migrate` | `migrate` |
| `kb_bootstrap` | `bootstrap` |
| `kb_bootstrap_raw` | `bootstrap-raw` |
| `kb_compile` | `compile` |
| `kb_evolve` | `evolve` |
| `kb_status` | `status` |
| `kb_query` | `query` |
| `kb_submit_outcome` | `outcome` |
| `kb_list_proposals` | `proposals` |
| `kb_promote` | `promote` |
| `kb_rollback` | `rollback` |
| `kb_lint` | `lint` |

Do not put Git credentials, repository URLs, personal paths, or company identifiers inside the portable skill. Store those in each harness's local configuration.
