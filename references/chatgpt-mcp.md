# ChatGPT MCP Access

This project includes a read-only MCP server for connecting a self-growing-kb
repository to ChatGPT or another MCP client.

## What It Exposes

- `search`: search active Wiki pages and permitted latest Raw evidence.
- `fetch`: fetch a Wiki page, Raw event, proposal, revision, or safe path.
- `wiki_search`: explicit alias for `search`.
- `wiki_fetch`: explicit alias for `fetch`.
- `wiki_recent_changes`: inspect recent committed repository changes.
- `wiki_list_proposals`: inspect pending, blocked, approved, or rejected proposals.
- `wiki_status`: inspect queue, page, revision, proposal, and gap counts.

The server does not expose write, promote, ingest, or delete tools. It also
blocks reads outside safe Wiki paths, including `.kb/private-credentials`.

## Local Run

```bash
cd /path/to/self-growing-kb
export WIKI_ROOT="/path/to/personal-wiki"
export WIKI_PRINCIPAL="personal-gjamsue"
export WIKI_MCP_TOKEN="$(openssl rand -hex 24)"
python3 scripts/wiki_mcp_server.py --host 127.0.0.1 --port 8766
```

Health check:

```bash
curl http://127.0.0.1:8766/health
```

Tool list check:

```bash
curl -s http://127.0.0.1:8766/mcp \
  -H "Authorization: Bearer $WIKI_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## ChatGPT Pro

ChatGPT needs a reachable HTTPS MCP endpoint. For a local personal wiki, run this
server locally and expose only `/mcp` through a secure MCP tunnel, Cloudflare
Tunnel, ngrok, or another HTTPS reverse proxy that you control.

Then connect the public HTTPS URL ending in `/mcp` from ChatGPT developer mode.
Use bearer authentication with the same `WIKI_MCP_TOKEN`.

Keep the server read-only for personal ChatGPT usage. Let scheduled Codex jobs or
explicit local commands perform ingestion, proposal creation, and promotion.
