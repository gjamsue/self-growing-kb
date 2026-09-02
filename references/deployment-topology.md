# Deployment Topology

Use this reference when deciding how personal and work knowledge bases attach to ChatGPT, Codex, Claude Code, and schedulers.

## Separation rule

Keep personal and work knowledge in separate repositories, credentials, schedules, and MCP deployments.

```text
personal-wiki private repo
  -> personal local scheduler
  -> personal ChatGPT Pro read/fetch connector

work-wiki private repo
  -> company local scheduler or worker
  -> company ChatGPT Enterprise MCP connector
  -> company Codex and Claude Code skill installs
```

Never route personal Gmail, WeChat, or personal raw evidence into a company workspace. Never route company Jira, Slack, Confluence, GitHub, Bitbucket, GDrive, or SharePoint evidence into a personal workspace unless the source owner explicitly exports a personal copy with permission.

## Personal deployment

Personal ChatGPT Pro should use a read/fetch/search MCP connector for the private personal wiki. Write-like operations can be exposed only when the ChatGPT plan and connector mode support them; otherwise keep writes in Codex/local automation.

Recommended personal duties:

- Local `launchd` or cron refreshes Gmail and WeChat.
- The local checkout commits and pushes accepted Raw Events and proposals to the private personal wiki repo.
- ChatGPT Pro reads `wiki_search`, `wiki_fetch`, `wiki_recent_changes`, and `wiki_list_proposals`.
- Codex can perform `wiki_remember`, `wiki_refresh`, and `wiki_promote` against the local checkout when the user explicitly asks.

## Work deployment

Company ChatGPT Enterprise can use a fuller MCP connector for the work wiki, subject to admin approval, RBAC, and workspace policy.

Recommended work duties:

- Work source connectors run on a company-controlled machine, CI runner, or developer workstation with approved credentials.
- Codex and Claude Code install the same `self-growing-kb` skill and operate on the same local work wiki checkout.
- ChatGPT Enterprise exposes read tools broadly only if policy allows; write tools such as remember, refresh, and promote should be role-gated.
- All updates go through Raw Events, proposals, promotion, lint, and Git audit trail.

## Local filesystem mode

When Codex or Claude Code has filesystem access, no remote gateway is needed for that harness:

```bash
python3 /path/to/self-growing-kb/scripts/kb.py query /path/to/work-wiki "question" --principal user-or-service
python3 /path/to/self-growing-kb/scripts/kb.py evolve /path/to/work-wiki
python3 /path/to/self-growing-kb/scripts/kb.py proposals /path/to/work-wiki --status pending
```

The skill teaches the agent to look for `kb.json`, preserve evidence boundaries, run lint, and avoid direct Wiki edits.

## ChatGPT mode

ChatGPT cannot directly read a local filesystem checkout. To use the wiki inside ChatGPT, provide a remote MCP server or an approved secure tunnel to a local server.

Map ChatGPT tools to CLI operations:

| ChatGPT/MCP tool | Use |
|---|---|
| `wiki_search` | Answer with active Wiki and latest Raw evidence |
| `wiki_fetch` | Fetch a specific page, proposal, or evidence record |
| `wiki_recent_changes` | Show new Raw Events, proposals, and revisions since a timestamp |
| `wiki_list_proposals` | Review pending proposals |
| `wiki_remember` | Create a user-authored Raw Event with candidates |
| `wiki_refresh` | Trigger approved local connectors |
| `wiki_promote` | Approve or reject a proposal, then lint |

For personal Pro, prefer read/fetch/search tools first. For Enterprise, enable write tools only after admin review and role gating.

## Scheduling

Periodic refresh belongs outside ordinary chat sessions:

```text
Gmail/IMAP/API, WeChat, Jira, Slack, Confluence, GDrive, SharePoint
  -> source connector with cursor or stable source version
  -> Raw Event ingest
  -> candidate extraction
  -> evolve
  -> lint
  -> git commit/push
```

Schedulers should be explicit per repository. Personal schedules may run on the user's Mac. Work schedules should run on company-approved infrastructure or a local developer machine if that is the company policy.

## Review stance

Automated refresh can create evidence and proposals. It should not silently approve durable facts, access changes, deletions, or conflict resolutions. Promotion remains a deliberate action unless the update is deterministic metadata allowed by the mutation policy.
