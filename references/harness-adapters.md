# Harness Adapters

Keep the CLI and repository protocol authoritative. Harness adapters should only translate prompts, tool calls, and lifecycle events.

## Codex

Install the `self-growing-kb` folder as a Skill or bundle it into a Codex plugin. Invoke `scripts/kb.py` for deterministic operations. Add an MCP wrapper only when remote or long-running access is needed.

## Claude Code

Expose the same `SKILL.md` under the harness's supported skills directory. Translate tool names in examples to shell execution. Keep `kb.py` unchanged.

## Gemini CLI

Install the instructions using the supported skill or extension mechanism. Keep the CLI JSON contract unchanged and map task completion to `outcome`.

## OpenCode and generic harnesses

Reference `SKILL.md` from the harness instruction file and allow execution of `python3 scripts/kb.py`. If the harness supports MCP, wrap the seven CLI operations without changing their payloads.

## Future MCP tool mapping

| MCP tool | CLI operation |
|---|---|
| `kb_init` | `init` |
| `kb_ingest` | `ingest` |
| `kb_query` | `query` |
| `kb_submit_outcome` | `outcome` |
| `kb_list_proposals` | `proposals` |
| `kb_promote` | `promote` |
| `kb_lint` | `lint` |

Do not put Git credentials, repository URLs, personal paths, or company identifiers inside the portable skill. Store those in each harness's local configuration.
