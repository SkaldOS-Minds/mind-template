#!/usr/bin/env python3
"""assert.py: the only sanctioned write path into the mind.

Validates, appends to graph/assertions.jsonl (the Edda log, design 4.2), then
re-projects.

Enforcement posture (design 4.4). Structural errors are blocked:
unknown assertion kind, unknown node type, unknown predicate, endpoint type
mismatch, unknown or closed-set-violating tag, tag_assign against a node that
does not exist, edge_archive with no active edge to archive, an unknown method,
an unknown confidence band, an unregistered source identifier, and any attempt
to supply `id` or `asserted_at` by hand.

Hygiene gaps are warnings only: no source and no lineage, no confidence band,
an edge endpoint that does not exist yet, a required-tag contract left unmet.
Warnings do not stop the write. hygiene.py measures what that costs.

Usage:
    scripts/assert.py node --id n:thing --type concept --title "A thing" \\
        --tag domain=strategy --band likely --source src:design-doc --method human --by aleksander
    scripts/assert.py edge --src n:a --predicate supports --dst n:b --band possible --source src:x
    scripts/assert.py archive --src n:a --predicate supports --dst n:b --reason "..." --source src:x
    scripts/assert.py tag --node n:thing --tag status=active --band certain --source src:x
    scripts/assert.py batch --file batch.jsonl        # rows of partial assertion IR
    ... any of the above with --dry-run to validate without writing.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402
import project  # noqa: E402


class Blocked(Exception):
    pass


def parse_tags(pairs):
    tags: dict = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise Blocked(f"--tag expects name=value, got '{pair}'")
        name, value = pair.split("=", 1)
        tags.setdefault(name, []).append(value)
    # single values stay scalar so the payload reads naturally
    return {k: (v if len(v) > 1 else v[0]) for k, v in tags.items()}


def provenance(args) -> dict:
    return {
        "confidence_band": args.band,
        "source_identifier": args.source,
        "derived_from": args.derived_from,
        "method": args.method,
        "by": args.by,
    }


def build_rows(args) -> list:
    if args.command == "node":
        node = {"id": args.id}
        if args.type:
            node["type"] = args.type
        if args.title:
            node["title"] = args.title
        tags = parse_tags(args.tag)
        if tags:
            node["tags"] = tags
        return [{"kind": "node_upsert", "node": node, **provenance(args)}]

    if args.command == "edge":
        return [{
            "kind": "edge_assert",
            "edge": {"src": args.src, "predicate": args.predicate, "dst": args.dst},
            **provenance(args),
        }]

    if args.command == "archive":
        return [{
            "kind": "edge_archive",
            "edge": {"src": args.src, "predicate": args.predicate, "dst": args.dst},
            "reason": args.reason,
            **provenance(args),
        }]

    if args.command == "tag":
        tags = parse_tags(args.tag)
        if len(tags) != 1:
            raise Blocked("tag takes exactly one --tag name=value")
        name, value = next(iter(tags.items()))
        return [{
            "kind": "tag_assign", "node_id": args.node, "tag": name, "value": value,
            **provenance(args),
        }]

    if args.command == "batch":
        if args.file:
            raw = lib.load_jsonl(Path(args.file))
        elif args.json:
            parsed = json.loads(args.json)
            raw = parsed if isinstance(parsed, list) else [parsed]
        else:
            raw = [json.loads(line) for line in sys.stdin.read().splitlines() if line.strip()]
        rows = []
        for r in raw:
            row = dict(r)
            for field in ("confidence_band", "source_identifier", "derived_from", "method", "by"):
                if field not in row:
                    default = {
                        "confidence_band": args.band, "source_identifier": args.source,
                        "derived_from": args.derived_from, "method": args.method, "by": args.by,
                    }[field]
                    row[field] = default
            rows.append(row)
        return rows

    raise Blocked(f"unknown command {args.command}")


def structural_checks(row, ontology, sources, known_nodes, active_edges):
    """Row-level checks the fold is deliberately permissive about."""
    errors, warnings = [], []
    kind = row.get("kind")
    if kind not in lib.ASSERTION_KINDS:
        errors.append(f"unknown assertion kind '{kind}' (expected one of {list(lib.ASSERTION_KINDS)})")
        return errors, warnings
    if "id" in row and row["id"]:
        errors.append("id is assigned by assert.py and must not be supplied")
    if row.get("asserted_at"):
        errors.append("asserted_at is fixed at write time and must not be supplied")
    if row.get("method") not in lib.METHODS:
        errors.append(f"method '{row.get('method')}' is not one of {list(lib.METHODS)}")

    band = row.get("confidence_band")
    if band is None:
        warnings.append("no confidence_band (hygiene gap)")
    elif band not in ontology.get("confidence_bands", {}):
        errors.append(f"unknown confidence band '{band}'")

    src_id = row.get("source_identifier")
    if src_id and src_id not in sources:
        errors.append(f"source_identifier '{src_id}' is not registered in graph/sources.jsonl")
    if not src_id and not row.get("derived_from"):
        warnings.append("no source_identifier and no derived_from (no lineage root; hygiene gap)")

    if kind == "node_upsert":
        node = row.get("node") or {}
        if not node.get("id"):
            errors.append("node_upsert needs node.id")
        elif node["id"] not in known_nodes:
            if not node.get("type"):
                errors.append(f"new node '{node['id']}' needs a type")
            if not node.get("title"):
                errors.append(f"new node '{node['id']}' needs a title")

    elif kind == "tag_assign":
        if not row.get("node_id"):
            errors.append("tag_assign needs node_id")
        if not row.get("tag"):
            errors.append("tag_assign needs tag")

    elif kind in ("edge_assert", "edge_archive"):
        edge = row.get("edge") or {}
        missing = [f for f in ("src", "predicate", "dst") if not edge.get(f)]
        if missing:
            errors.append(f"{kind} needs edge.{', edge.'.join(missing)}")
        else:
            key = lib.edge_key(edge)
            if kind == "edge_archive" and key not in active_edges:
                errors.append(f"edge_archive target '{key}' has no active edge to archive")
            for side in ("src", "dst"):
                if edge[side] not in known_nodes:
                    warnings.append(f"edge {side} '{edge[side]}' is not a known node (orphan edge)")
    return errors, warnings


def contract_warnings(nodes, ontology):
    out = []
    contracts = ontology.get("contracts", {})
    for nid in sorted(nodes):
        node = nodes[nid]
        spec = contracts.get(node.get("type"))
        if not spec:
            continue
        for tag in spec.get("required_tags", []):
            if tag not in node["tags"]:
                out.append(f"{nid} ({node.get('type')}) is missing required tag '{tag}'")
    return out


def validate(pending, existing, ontology, sources):
    """Returns (errors, warnings), each a list of (index, message)."""
    errors, warnings = [], []
    base_nodes, base_edges, base_problems = project.fold(existing, ontology)
    base_contract = set(contract_warnings(base_nodes, ontology))
    seen_problems = list(base_problems)
    accumulated = list(existing)

    known_nodes = dict(base_nodes)
    active_edges = {k for k, e in base_edges.items() if e["active"]}

    for i, row in enumerate(pending):
        errs, warns = structural_checks(row, ontology, sources, known_nodes, active_edges)
        for e in errs:
            errors.append((i, e))
        for w in warns:
            warnings.append((i, w))

        # Fold this row in with a provisional id so the fold's own structural
        # checks (unknown type, unknown predicate, endpoint mismatch, closed-set
        # tags) apply to it exactly as check.py will apply them later.
        provisional = dict(row)
        provisional["id"] = row.get("id") or f"row {i}"
        provisional["asserted_at"] = row.get("asserted_at") or lib.iso_now()
        accumulated.append(provisional)
        nodes, edges, problems = project.fold(accumulated, ontology)
        # The endpoint sweep re-runs on every fold, so diff problems by content
        # rather than by position: anything new is this row's fault.
        counted = list(seen_problems)
        for p in problems:
            if p in counted:
                counted.remove(p)
            else:
                errors.append((i, p))
        seen_problems = problems
        known_nodes = nodes
        active_edges = {k for k, e in edges.items() if e["active"]}

    # Contracts are judged on the state the whole batch leaves behind, so a
    # node created in row 0 and tagged in row 3 does not warn.
    for w in sorted(set(contract_warnings(known_nodes, ontology)) - base_contract):
        warnings.append((len(pending) - 1, f"contract: {w}"))

    return errors, warnings


def main():
    ap = argparse.ArgumentParser(
        description="Validate and append an assertion, then re-project.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--band", "--confidence-band", dest="band",
                    choices=["certain", "likely", "possible", "speculative"])
    ap.add_argument("--source", "--source-identifier", dest="source")
    ap.add_argument("--derived-from", dest="derived_from")
    ap.add_argument("--method", default=os.environ.get("MIND_METHOD", "agent"),
                    choices=list(lib.METHODS))
    ap.add_argument("--by", default=os.environ.get("MIND_PRINCIPAL", "unattributed-agent"))
    ap.add_argument("--dry-run", action="store_true", help="validate and print, write nothing")
    ap.add_argument("--no-project", action="store_true", help="append without re-projecting")

    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("node", help="upsert a node")
    p.add_argument("--id", required=True)
    p.add_argument("--type")
    p.add_argument("--title")
    p.add_argument("--tag", action="append", metavar="NAME=VALUE")

    p = sub.add_parser("edge", help="assert an edge")
    p.add_argument("--src", required=True)
    p.add_argument("--predicate", required=True)
    p.add_argument("--dst", required=True)

    p = sub.add_parser("archive", help="archive an active edge")
    p.add_argument("--src", required=True)
    p.add_argument("--predicate", required=True)
    p.add_argument("--dst", required=True)
    p.add_argument("--reason", required=True)

    p = sub.add_parser("tag", help="assign a tag to a node")
    p.add_argument("--node", required=True)
    p.add_argument("--tag", action="append", required=True, metavar="NAME=VALUE")

    p = sub.add_parser("batch", help="append many rows of assertion IR")
    p.add_argument("--file")
    p.add_argument("--json")

    args = ap.parse_args()

    try:
        rows = build_rows(args)
    except Blocked as exc:
        print(f"assert.py: blocked: {exc}", file=sys.stderr)
        return 2

    if not rows:
        print("assert.py: nothing to write", file=sys.stderr)
        return 2

    ontology = lib.load_ontology()
    sources = lib.load_sources()
    existing = lib.load_assertions()

    errors, warnings = validate(rows, existing, ontology, sources)

    for i, w in warnings:
        print(f"assert.py: warning [row {i}]: {w}", file=sys.stderr)
    if errors:
        for i, e in errors:
            print(f"assert.py: BLOCKED [row {i}]: {e}", file=sys.stderr)
        print(f"assert.py: {len(errors)} structural error(s); nothing was written", file=sys.stderr)
        return 2

    # ids and asserted_at are assigned here, once, and never regenerated.
    # Ids are UUIDv7 (`a-<uuidv7>`), monotonic within the batch, so two
    # writers cannot collide and the batch keeps its order under the fold key.
    now = lib.iso_now()
    numbered = []
    for row in rows:
        final = {"id": lib.new_assertion_id(), "kind": row["kind"]}
        for field in ("node", "edge", "node_id", "tag", "value", "reason"):
            if field in row:
                final[field] = row[field]
        final["confidence_band"] = row.get("confidence_band")
        final["source_identifier"] = row.get("source_identifier")
        final["derived_from"] = row.get("derived_from")
        final["method"] = row.get("method")
        final["by"] = row.get("by")
        final["asserted_at"] = now
        for field in row:
            if field not in lib.KNOWN_ASSERTION_FIELDS:
                final[field] = row[field]
        numbered.append(final)

    if args.dry_run:
        print("assert.py --dry-run: would append")
        for row in numbered:
            print("  " + json.dumps(row, ensure_ascii=False))
        return 0

    if lib.ensure_edda_header():
        print(f'assert.py: wrote the Edda header record (edda/{lib.EDDA_VERSION})')
    lib.append_jsonl(lib.ASSERTIONS_PATH, numbered)
    print(f"assert.py: appended {len(numbered)} assertion(s): "
          f"{', '.join(r['id'] for r in numbered)}")

    if not args.no_project:
        project.write(project.compute(), quiet=True)
        print("assert.py: re-projected nodes, edges and vault "
              "(run scripts/dream.py to refresh beliefs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
