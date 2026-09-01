#!/usr/bin/env python3
"""Command-line interface for the self-growing-kb skill."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kb_core import (
    KBError,
    compile_pending,
    evolve_kb,
    ingest_event,
    init_kb,
    lint_kb,
    list_proposals,
    migrate_kb,
    promote_proposal,
    query_kb,
    read_json,
    status_kb,
    submit_outcome,
)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="kb", description="Portable self-growing knowledge base CLI")
    sub = root.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Initialize an empty knowledge repository")
    init.add_argument("root", type=Path)
    init.add_argument("--profile", choices=("personal", "work"), required=True)
    init.add_argument("--tenant-id", required=True)

    ingest = sub.add_parser("ingest", help="Ingest a Normalized Raw Event JSON file")
    ingest.add_argument("root", type=Path)
    ingest.add_argument("event", type=Path)

    migrate = sub.add_parser("migrate", help="Upgrade a knowledge repository schema in place")
    migrate.add_argument("root", type=Path)

    compile_command = sub.add_parser("compile", help="Compile pending Raw Events into proposals")
    compile_command.add_argument("root", type=Path)
    compile_command.add_argument("--limit", type=int, default=100)

    evolve = sub.add_parser("evolve", help="Process incremental updates and report repository health")
    evolve.add_argument("root", type=Path)
    evolve.add_argument("--limit", type=int, default=100)

    status = sub.add_parser("status", help="Show queue, gaps, usage, and proposal status")
    status.add_argument("root", type=Path)

    query = sub.add_parser("query", help="Search Wiki and permitted Raw evidence")
    query.add_argument("root", type=Path)
    query.add_argument("question")
    query.add_argument("--principal", required=True)
    query.add_argument("--limit", type=int, default=8)
    query.add_argument("--wiki-only", action="store_true")

    outcome = sub.add_parser("outcome", help="Submit an Agent outcome and create update proposals")
    outcome.add_argument("root", type=Path)
    outcome.add_argument("outcome", type=Path)

    proposals = sub.add_parser("proposals", help="List update proposals")
    proposals.add_argument("root", type=Path)
    proposals.add_argument("--status", choices=("pending", "blocked", "approved", "rejected"))

    promote = sub.add_parser("promote", help="Approve or reject a proposal")
    promote.add_argument("root", type=Path)
    promote.add_argument("proposal_id")
    promote.add_argument("--action", choices=("approve", "reject"), required=True)
    promote.add_argument("--reviewer", required=True)

    lint = sub.add_parser("lint", help="Validate repository structure and records")
    lint.add_argument("root", type=Path)
    return root


def run(args: argparse.Namespace) -> object:
    if args.command == "init":
        return init_kb(args.root.resolve(), args.profile, args.tenant_id)
    if args.command == "ingest":
        return ingest_event(args.root.resolve(), read_json(args.event.resolve()))
    if args.command == "migrate":
        return migrate_kb(args.root.resolve())
    if args.command in {"compile", "evolve"} and not 1 <= args.limit <= 10000:
        raise KBError("limit must be between 1 and 10000")
    if args.command == "compile":
        return compile_pending(args.root.resolve(), args.limit)
    if args.command == "evolve":
        return evolve_kb(args.root.resolve(), args.limit)
    if args.command == "status":
        return status_kb(args.root.resolve())
    if args.command == "query":
        if args.limit < 1 or args.limit > 100:
            raise KBError("limit must be between 1 and 100")
        return query_kb(args.root.resolve(), args.question, args.principal, args.limit, not args.wiki_only)
    if args.command == "outcome":
        return submit_outcome(args.root.resolve(), read_json(args.outcome.resolve()))
    if args.command == "proposals":
        return {"proposals": list_proposals(args.root.resolve(), args.status)}
    if args.command == "promote":
        return promote_proposal(args.root.resolve(), args.proposal_id, args.action, args.reviewer)
    if args.command == "lint":
        result = lint_kb(args.root.resolve())
        if not result["ok"]:
            raise KBError(json.dumps(result, ensure_ascii=False))
        return result
    raise KBError(f"Unknown command: {args.command}")


def main() -> int:
    try:
        result = run(parser().parse_args())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KBError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
