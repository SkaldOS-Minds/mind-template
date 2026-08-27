#!/usr/bin/env python3
"""promote.py: replay this mind's assertion log into a SkaldOS tenant.

P0 ships the dry run only. `--dry-run` streams the Edda log
(graph/assertions.jsonl, edda/0) through the field mapping in design section
10.2 and prints the assert-IR batches that would be POSTed to /graph/assert,
plus a report of anything the mapping does not carry. Wiring --target to the
staging endpoint is P1.5.

Mapping, Edda field to factory target (design 10.2):

    kind: node_upsert    -> claim kind "node"
    kind: edge_assert    -> claim kind "edge"
    kind: edge_archive   -> claim kind "edge_archive"
    kind: tag_assign     -> claim kind "tag_assignment"
    confidence_band      -> assertion confidence band
    source_identifier    -> AssertionSource.identifier
    derived_from         -> derived_from_node_id, resolved through the batch symbol map
    asserted_at          -> valid_from, fixed (idempotency rule)
    by + method          -> principal attribution on the write
    ontology.json        -> an Ontology Studio draft, published before replay

Does not carry: git commit history (a provenance note node at most), vault note
bodies (document nodes if wanted), Actions history, hub membership rows (they
become real memberships through the invitation flow).

Usage:
    python3 scripts/promote.py --dry-run
    python3 scripts/promote.py --dry-run --batch-size 25 --out /tmp/batches.json
    python3 scripts/promote.py --dry-run --summary
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

CLAIM_KIND = {
    "node_upsert": "node",
    "edge_assert": "edge",
    "edge_archive": "edge_archive",
    "tag_assign": "tag_assignment",
}

NOT_CARRIED = [
    "git commit history (summarized into a provenance note node at most)",
    "vault note bodies (imported as document nodes if wanted)",
    "GitHub Actions history",
    "hub membership rows (recreated through the invitation flow)",
    "derived files (nodes, edges, projections; the tenant re-derives them)",
]


def symbol_map(assertions) -> dict:
    """assertion id -> the node external id that assertion introduced. This is
    how `derived_from: a-0005` becomes `derived_from_node_id` on the target."""
    out = {}
    for a in assertions:
        if a.get("kind") == "node_upsert":
            nid = (a.get("node") or {}).get("id")
            if nid:
                out[a["id"]] = nid
        elif a.get("kind") == "tag_assign" and a.get("node_id"):
            out[a["id"]] = a["node_id"]
        elif a.get("kind") in ("edge_assert", "edge_archive"):
            edge = a.get("edge") or {}
            if edge.get("src"):
                out[a["id"]] = edge["src"]
    return out


def map_assertion(a, symbols, unmapped) -> dict:
    kind = a.get("kind")
    claim = {
        "claim_kind": CLAIM_KIND.get(kind),
        "mind_assertion_id": a.get("id"),
    }
    if kind == "node_upsert":
        node = a.get("node") or {}
        claim["node"] = {
            "external_id": node.get("id"),
            "type": node.get("type"),
            "title": node.get("title"),
            "tags": node.get("tags") or {},
        }
    elif kind in ("edge_assert", "edge_archive"):
        edge = a.get("edge") or {}
        claim["edge"] = {
            "src_external_id": edge.get("src"),
            "predicate": edge.get("predicate"),
            "dst_external_id": edge.get("dst"),
        }
        if kind == "edge_archive":
            claim["archive_reason"] = a.get("reason")
    elif kind == "tag_assign":
        claim["tag_assignment"] = {
            "node_external_id": a.get("node_id"),
            "tag": a.get("tag"),
            "value": a.get("value"),
        }
    else:
        unmapped.append(f"{a.get('id')}: unknown kind '{kind}' has no claim mapping")

    claim["confidence_band"] = a.get("confidence_band")
    source = a.get("source_identifier")
    claim["source"] = {"identifier": source} if source else None

    derived = a.get("derived_from")
    if not derived:
        claim["derived_from_node_id"] = None
    elif derived.startswith("src:"):
        # a lineage anchored straight on a source, not on another claim
        claim["derived_from_node_id"] = None
        claim["derived_from_source_identifier"] = derived
    else:
        resolved = symbols.get(derived)
        claim["derived_from_node_id"] = resolved
        if resolved is None:
            unmapped.append(
                f"{a.get('id')}: derived_from '{derived}' resolves to no node in the symbol map")

    claim["valid_from"] = a.get("asserted_at")
    claim["principal"] = {"label": a.get("by"), "method": a.get("method")}

    for field in a:
        if field not in lib.KNOWN_ASSERTION_FIELDS:
            unmapped.append(f"{a.get('id')}: field '{field}' has no target in the mapping")
    return claim


def build_batches(assertions, batch_size):
    symbols = symbol_map(assertions)
    unmapped = []
    claims = [map_assertion(a, symbols, unmapped) for a in assertions]
    batches = []
    for i in range(0, len(claims), batch_size):
        chunk = claims[i:i + batch_size]
        batches.append({
            "batch": len(batches) + 1,
            "source_format": f"edda/{lib.EDDA_VERSION}",
            "endpoint": "POST /graph/assert",
            "idempotency_key": f"mind-replay-{chunk[0]['mind_assertion_id']}-{chunk[-1]['mind_assertion_id']}",
            "claims": chunk,
        })
    return batches, unmapped


def main():
    ap = argparse.ArgumentParser(description="Replay the assertion log into a tenant (dry run).")
    ap.add_argument("--dry-run", action="store_true", required=True,
                    help="the only mode P0 supports; live replay lands in P1.5")
    ap.add_argument("--batch-size", type=int, default=50)
    ap.add_argument("--target", help="assert endpoint; recorded but never called in P0")
    ap.add_argument("--out", help="write the batches to a file instead of stdout")
    ap.add_argument("--summary", action="store_true", help="counts and gaps only")
    args = ap.parse_args()

    header, assertions = lib.split_edda_log()
    ontology = lib.load_ontology()
    if header is None:
        print("promote.py: the log has no Edda header record; run scripts/check.py",
              file=sys.stderr)
        return 1
    batches, unmapped = build_batches(assertions, args.batch_size)

    counts = {}
    for a in assertions:
        counts[a.get("kind")] = counts.get(a.get("kind"), 0) + 1

    if not args.summary:
        payload = json.dumps(batches, ensure_ascii=False, indent=2)
        if args.out:
            Path(args.out).write_text(payload + "\n")
            print(f"promote.py: wrote {len(batches)} batch(es) to {args.out}")
        else:
            print(payload)

    print(f"\npromote.py --dry-run: mind '{header.get('mind')}', edda/{header.get('edda')}, "
          f"{len(assertions)} assertions -> {len(batches)} batch(es) "
          f"of at most {args.batch_size}", file=sys.stderr)
    for kind in sorted(counts):
        print(f"  {kind}: {counts[kind]} -> claim kind '{CLAIM_KIND.get(kind, '?')}'", file=sys.stderr)
    print(f"  ontology v{ontology.get('version')}: publish as an Ontology Studio draft before replay",
          file=sys.stderr)
    print(f"  target: {args.target or 'not set (P1.5 wires staging /graph/assert)'}", file=sys.stderr)
    print("  does not carry:", file=sys.stderr)
    for item in NOT_CARRIED:
        print(f"    - {item}", file=sys.stderr)
    if unmapped:
        print(f"  unmapped fields ({len(unmapped)}):", file=sys.stderr)
        for u in unmapped:
            print(f"    - {u}", file=sys.stderr)
        return 1
    print("  unmapped fields: none", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
