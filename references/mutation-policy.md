# Knowledge Mutation Policy

All Agent-originated knowledge changes are proposals first.

| Decision | Typical changes | Default handling |
|---|---|---|
| `auto_commit` | Add source, link, tag, usage count, stale marker | May be applied by deterministic tooling |
| `draft` | New node, node expansion, evidence-backed update | Create a draft; promote after review |
| `review_required` | Rewrite fact, resolve conflict, delete, ACL or policy change, durable judgment | Require an identified human reviewer |
| `blocked` | No evidence, unknown permission, cross-repository movement | Resolve the blocking condition and create a new proposal |

## Invariants

- Require at least one evidence reference for every mutation.
- Require `permission_scope` to be known.
- Never approve a blocked proposal in place.
- Never silently overwrite an existing Wiki node.
- Never expand visibility beyond the intersection allowed by supporting sources.
- Never transfer personal and work content through the ordinary Promote path.

## Cross-repository promotion

Treat transfer between personal and work repositories as a separate export workflow:

1. Select a generalizable method, never a source document.
2. Remove names, identifiers, links, quotes, metrics, and confidential context.
3. Create a new artifact with an explicit destination tenant.
4. Review it under the destination repository's policy.
5. Ingest it as a new source in the destination; do not preserve source ACL assumptions.

This skill does not automate cross-repository export.
