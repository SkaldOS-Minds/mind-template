#!/usr/bin/env python3
"""dream.py: the epistemic layer, method evidence_weighted/v0 (design section 5).

For every active edge whose predicate is not provenance-kind:

  1. Gather every edge_assert as evidence and every edge_archive as
     counter-evidence, from the full history of that edge.
  2. Resolve each assertion to a root source through source_identifier /
     derived_from lineage. No resolvable, registered root means the assertion
     contributes nothing.
  3. Weight = source reliability prior x confidence-band weight. Group by root
     source and take the maximum per source, so repeating yourself is not
     corroboration.
  4. Combine independent sources noisy-OR style into a support score, and the
     same for counter-evidence.
  5. Emit one projection row per edge with a status, the independent support
     count, the contributing sources, and the method name and version.

Honesty rule: no resolvable evidence yields insufficient_evidence, never a
score. Rows carry no timestamp, so `git diff graph/projections.jsonl` shows only
what the mind actually changed its mind about. Run history lands in
graph/dream-history.jsonl instead.

Usage:
    python3 scripts/dream.py                # dream, write projections, refresh vault
    python3 scripts/dream.py --check        # report drift, write nothing (exit 1 on drift)
    python3 scripts/dream.py --explain n:a  # show the evidence behind edges touching a node
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402
import project  # noqa: E402


def noisy_or(weights) -> float:
    product = 1.0
    for w in weights:
        product *= (1.0 - w)
    return 1.0 - product


def weigh(assertion, by_id, sources, ontology, cfg):
    """(root_source_id, weight) or (None, reason) when nothing is resolvable."""
    root = lib.resolve_root_source(assertion, by_id)
    if root is None:
        return None, "no resolvable lineage root"
    source = sources.get(root)
    if source is None:
        return None, f"root source '{root}' is not registered in sources.jsonl"
    reliability = source.get("reliability")
    if reliability is None:
        return None, f"root source '{root}' has no reliability prior"
    band = assertion.get("confidence_band")
    bw = lib.band_weight(ontology, band)
    if bw is None:
        bw = cfg["unbanded_weight"]
    return root, round(float(reliability) * float(bw), 6)


def collect(assertion_ids, by_id, sources, ontology, cfg):
    """Max weight per root source, plus the assertions that could not be used."""
    per_source: dict = {}
    contributing: dict = {}
    unusable = []
    for aid in assertion_ids:
        a = by_id.get(aid)
        if a is None:
            unusable.append({"assertion": aid, "reason": "assertion id not in the log"})
            continue
        root, value = weigh(a, by_id, sources, ontology, cfg)
        if root is None:
            unusable.append({"assertion": aid, "reason": value})
            continue
        if value > per_source.get(root, -1.0):
            per_source[root] = value
            contributing[root] = aid
    return per_source, contributing, unusable


def classify(support, counter, count, cfg) -> str:
    if support is None and counter is None:
        return "insufficient_evidence"
    s = support or 0.0
    c = counter or 0.0
    if s >= cfg["contest_support_floor"] and c >= cfg["contest_counter_floor"]:
        return "contested"
    if s >= cfg["support_threshold"] and count >= cfg["min_independent_sources"]:
        return "supported"
    return "undetermined"


def dream(assertions=None, ontology=None, sources=None):
    ontology = ontology or lib.load_ontology()
    assertions = assertions if assertions is not None else lib.load_assertions()
    sources = sources if sources is not None else lib.load_sources()
    cfg = lib.dream_config(ontology)
    by_id = lib.index_by_id(assertions)

    nodes, edges, _ = project.fold(assertions, ontology)
    rows = []
    skipped_provenance = 0

    for key in sorted(edges, key=lambda k: (edges[k]["src"], edges[k]["predicate"], edges[k]["dst"])):
        e = edges[key]
        if not e["active"]:
            continue
        if lib.predicate_kind(ontology, e["predicate"]) == "provenance":
            skipped_provenance += 1
            continue

        sup_per_source, sup_contrib, sup_unusable = collect(
            e["asserts"], by_id, sources, ontology, cfg)
        cnt_per_source, cnt_contrib, cnt_unusable = collect(
            e["archives"], by_id, sources, ontology, cfg)

        support = round(noisy_or(sup_per_source.values()), 6) if sup_per_source else None
        counter = round(noisy_or(cnt_per_source.values()), 6) if cnt_per_source else None
        count = len(sup_per_source)
        status = classify(support, counter, count, cfg)

        rows.append({
            "edge_key": key,
            "src": e["src"],
            "predicate": e["predicate"],
            "dst": e["dst"],
            "predicate_kind": lib.predicate_kind(ontology, e["predicate"]),
            "status": status,
            "support_score": support,
            "counter_score": counter,
            "independent_support_count": count,
            "independent_counter_count": len(cnt_per_source),
            "supporting_sources": [
                {"source": s, "weight": sup_per_source[s], "assertion": sup_contrib[s]}
                for s in sorted(sup_per_source)
            ],
            "counter_sources": [
                {"source": s, "weight": cnt_per_source[s], "assertion": cnt_contrib[s]}
                for s in sorted(cnt_per_source)
            ],
            "unusable_evidence": sup_unusable + cnt_unusable,
            "method": cfg["method"],
            "method_version": cfg["version"],
        })

    summary = {
        "projections": len(rows),
        "provenance_edges_excluded": skipped_provenance,
        "nodes": len(nodes),
        "by_status": {},
    }
    for r in rows:
        summary["by_status"][r["status"]] = summary["by_status"].get(r["status"], 0) + 1
    return rows, summary, cfg


def main():
    ap = argparse.ArgumentParser(description="Run the epistemic projection.")
    ap.add_argument("--check", action="store_true", help="report drift, write nothing")
    ap.add_argument("--explain", metavar="NODE_ID", help="show the evidence behind a node's edges")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rows, summary, cfg = dream()

    if args.explain:
        hits = [r for r in rows if args.explain in (r["src"], r["dst"])]
        if not hits:
            print(f"no active non-provenance edges touch {args.explain}")
            return 0
        for r in hits:
            print(f"{r['edge_key']}")
            print(f"  status: {r['status']}  support={r['support_score']}  counter={r['counter_score']}")
            for s in r["supporting_sources"]:
                print(f"    + {s['source']} weight={s['weight']} via {s['assertion']}")
            for s in r["counter_sources"]:
                print(f"    - {s['source']} weight={s['weight']} via {s['assertion']}")
            for u in r["unusable_evidence"]:
                print(f"    ? {u['assertion']}: {u['reason']}")
        return 0

    text = lib.render_jsonl(rows)
    current = lib.PROJECTIONS_PATH.read_text() if lib.PROJECTIONS_PATH.exists() else None

    if args.check:
        if current != text:
            print("dream.py --check: graph/projections.jsonl drifts from the log")
            return 1
        if not args.quiet:
            print("dream.py --check: projections match the log")
        return 0

    changed = current != text
    lib.PROJECTIONS_PATH.write_text(text)

    if changed:
        lib.append_jsonl(lib.DREAM_HISTORY_PATH, [{
            "ran_at": lib.iso_now(),
            "method": cfg["method"],
            "method_version": cfg["version"],
            "projections": summary["projections"],
            "by_status": {k: summary["by_status"][k] for k in sorted(summary["by_status"])},
            "provenance_edges_excluded": summary["provenance_edges_excluded"],
        }])

    # Projection status is shown in the vault edge blocks, so re-fold after dreaming.
    project.write(project.compute(), quiet=True)

    if not args.quiet:
        print(f"dream.py: {cfg['method']}/{cfg['version']} over {summary['projections']} active edges")
        for status in sorted(summary["by_status"]):
            print(f"  {status}: {summary['by_status'][status]}")
        if summary["provenance_edges_excluded"]:
            print(f"  provenance edges excluded: {summary['provenance_edges_excluded']}")
        print("  projections changed" if changed else "  projections unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
