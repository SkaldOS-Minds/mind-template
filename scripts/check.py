#!/usr/bin/env python3
"""check.py: the invariant sweep. Everything the write path cannot prevent.

Errors (exit 1):
  - log integrity: duplicate or malformed assertion ids, unknown kinds,
    missing asserted_at, rows that were written around assert.py
  - structural problems found by the fold: unknown node type, unknown
    predicate, endpoint type mismatch, closed-set and cardinality violations
  - orphan edges: an endpoint that no node_upsert ever created
  - contract violations: a node type whose required tags are unmet
  - unknown sources: a source_identifier absent from graph/sources.jsonl, or a
    source registered with an unknown kind or an out-of-range reliability
  - derived-file drift: nodes.jsonl, edges.jsonl, projections.jsonl or any
    vault note whose machine-owned regions differ from a fresh fold

Warnings (exit 0):
  - hygiene gaps: assertions with no source and no resolvable lineage root,
    assertions with no confidence band
  - vault notes with no node behind them
  - nodes with no active edges

Usage:
    python3 scripts/check.py [--quiet] [--warnings-as-errors]
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402
import project  # noqa: E402
import dream as dream_mod  # noqa: E402


def check_edda(errors, warnings):
    """The log must self-identify as an Edda log (design 4.2)."""
    header, rows = lib.split_edda_log()
    if header is None:
        if not rows:
            # A mind stamped from the template, not yet folded once. Nothing is
            # wrong with it; it has simply never been run.
            warnings.append("edda: the log is empty and has no header record yet; "
                            "run scripts/project.py once to initialise this mind")
            return
        errors.append("edda: graph/assertions.jsonl has no header record; "
                      'the first line must be {"edda": "0", "mind": "<slug>"}')
        return
    if header.get("edda") != lib.EDDA_VERSION:
        errors.append(f"edda: log declares edda/{header.get('edda')}, "
                      f"these scripts implement edda/{lib.EDDA_VERSION}")
    if not header.get("mind"):
        warnings.append("edda: header record names no mind slug")
    for i, row in enumerate(rows):
        if "edda" in row:
            errors.append(f"edda: a second header record appears at line {i + 2}")


def check_log(assertions, errors, warnings):
    seen = set()
    for i, a in enumerate(assertions):
        where = a.get("id") or f"row {i}"
        aid = a.get("id")
        if not aid:
            errors.append(f"log: {where} has no id")
        elif not re.fullmatch(r"a-\d{4,}", aid):
            errors.append(f"log: id '{aid}' does not match a-NNNN")
        elif aid in seen:
            errors.append(f"log: duplicate assertion id '{aid}'")
        else:
            seen.add(aid)
        if a.get("kind") not in lib.ASSERTION_KINDS:
            errors.append(f"log: {where} has unknown kind '{a.get('kind')}'")
        if not a.get("asserted_at"):
            errors.append(f"log: {where} has no asserted_at")
        if a.get("method") not in lib.METHODS:
            errors.append(f"log: {where} has method '{a.get('method')}', expected one of {list(lib.METHODS)}")
        if not a.get("by"):
            warnings.append(f"log: {where} has no principal in 'by'")
        for field in a:
            if field not in lib.KNOWN_ASSERTION_FIELDS:
                warnings.append(f"log: {where} carries unmapped field '{field}' (promote.py will report it)")


def check_sources(assertions, sources, ontology, errors, warnings):
    kinds = set(ontology.get("source_kinds", []))
    for sid in sorted(sources):
        s = sources[sid]
        if s.get("kind") not in kinds:
            errors.append(f"sources: '{sid}' has unknown kind '{s.get('kind')}'")
        r = s.get("reliability")
        if not isinstance(r, (int, float)) or not 0.0 <= float(r) <= 1.0:
            errors.append(f"sources: '{sid}' reliability {r!r} is not in [0, 1]")
    for a in assertions:
        sid = a.get("source_identifier")
        if sid and sid not in sources:
            errors.append(f"sources: {a.get('id')} references unregistered source '{sid}'")


def check_graph(nodes, edges, ontology, errors, warnings):
    for key in sorted(edges):
        e = edges[key]
        for side in ("src", "dst"):
            if e[side] not in nodes:
                errors.append(f"orphan edge: {key} has no node for {side} '{e[side]}'")
    contracts = ontology.get("contracts", {})
    for nid in sorted(nodes):
        n = nodes[nid]
        spec = contracts.get(n.get("type"))
        if spec:
            for tag in spec.get("required_tags", []):
                if tag not in n["tags"]:
                    errors.append(
                        f"contract: {nid} (type {n['type']}) is missing required tag '{tag}'")
    touched = set()
    for e in edges.values():
        if e["active"]:
            touched.add(e["src"])
            touched.add(e["dst"])
    for nid in sorted(nodes):
        if nid not in touched:
            warnings.append(f"isolated node: {nid} has no active edges")


def check_hygiene(assertions, sources, ontology, warnings):
    by_id = lib.index_by_id(assertions)
    bands = ontology.get("confidence_bands", {})
    for a in assertions:
        aid = a.get("id")
        root = lib.resolve_root_source(a, by_id) if aid else None
        if root is None:
            warnings.append(f"hygiene: {aid} has no resolvable lineage root")
        elif root not in sources:
            warnings.append(f"hygiene: {aid} resolves to unregistered root '{root}'")
        if a.get("confidence_band") not in bands:
            warnings.append(f"hygiene: {aid} has no confidence band")


def check_derived(errors):
    result = project.compute()
    for item in project.drift(result):
        if item.endswith("(no node in the log)"):
            continue
        errors.append(f"drift: {item} differs from a fresh fold of the log")
    rows, _, _ = dream_mod.dream()
    text = lib.render_jsonl(rows)
    current = lib.PROJECTIONS_PATH.read_text() if lib.PROJECTIONS_PATH.exists() else None
    if current is None:
        errors.append("drift: graph/projections.jsonl is missing (run scripts/dream.py)")
    elif current != text:
        errors.append("drift: graph/projections.jsonl differs from a fresh dream")
    return result


def main():
    ap = argparse.ArgumentParser(description="Invariant sweep over the mind.")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--warnings-as-errors", action="store_true")
    args = ap.parse_args()

    ontology = lib.load_ontology()
    assertions = lib.load_assertions()
    sources = lib.load_sources()
    errors, warnings = [], []

    check_edda(errors, warnings)
    check_log(assertions, errors, warnings)
    nodes, edges, problems = project.fold(assertions, ontology)
    for p in problems:
        errors.append(f"fold: {p}")
    check_sources(assertions, sources, ontology, errors, warnings)
    check_graph(nodes, edges, ontology, errors, warnings)
    check_hygiene(assertions, sources, ontology, warnings)
    result = check_derived(errors)
    for item in project.drift(result):
        if item.endswith("(no node in the log)"):
            warnings.append(f"vault: {item}")

    if not args.quiet:
        for w in warnings:
            print(f"warning: {w}")
    for e in errors:
        print(f"ERROR: {e}", file=sys.stderr)

    if not args.quiet:
        print(f"check.py: {len(assertions)} assertions, {len(nodes)} nodes, {len(edges)} edges, "
              f"{len(errors)} error(s), {len(warnings)} warning(s)")
    if errors:
        return 1
    if warnings and args.warnings_as_errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
