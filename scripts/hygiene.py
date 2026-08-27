#!/usr/bin/env python3
"""hygiene.py: the measurement (design section 12).

The percentage of assertions carrying a source identifier, a resolvable lineage
root, and a banded confidence, split by method (human / agent / ingest). This is
the provenance-hygiene table run against prompt-only enforcement. The gap
between this number and a gated write path is the quantified value of the
chokepoint, so it is a time series from day one: every run appends a dated row
to graph/hygiene-history.jsonl.

A run whose numbers match the last row for the same date appends nothing, so
re-running the script is free and the history stays a record of change.

Usage:
    python3 scripts/hygiene.py               # measure, print, append if changed
    python3 scripts/hygiene.py --no-append   # measure and print only
    python3 scripts/hygiene.py --history     # print the series
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402


def pct(numerator, denominator) -> float:
    if not denominator:
        return 0.0
    return round(100.0 * numerator / denominator, 2)


def measure(assertions=None, sources=None, ontology=None) -> dict:
    ontology = ontology or lib.load_ontology()
    assertions = assertions if assertions is not None else lib.load_assertions()
    sources = sources if sources is not None else lib.load_sources()
    bands = ontology.get("confidence_bands", {})
    by_id = lib.index_by_id(assertions)

    buckets: dict = {"__all__": {"count": 0, "source": 0, "root": 0, "band": 0, "complete": 0}}
    for a in assertions:
        method = a.get("method") or "unattributed"
        for key in ("__all__", method):
            buckets.setdefault(key, {"count": 0, "source": 0, "root": 0, "band": 0, "complete": 0})
        has_source = bool(a.get("source_identifier")) and a["source_identifier"] in sources
        root = lib.resolve_root_source(a, by_id) if a.get("id") else None
        has_root = root is not None and root in sources
        has_band = a.get("confidence_band") in bands
        for key in ("__all__", method):
            b = buckets[key]
            b["count"] += 1
            b["source"] += int(has_source)
            b["root"] += int(has_root)
            b["band"] += int(has_band)
            b["complete"] += int(has_source and has_root and has_band)

    def score(b):
        return {
            "count": b["count"],
            "with_source_pct": pct(b["source"], b["count"]),
            "with_resolvable_root_pct": pct(b["root"], b["count"]),
            "with_band_pct": pct(b["band"], b["count"]),
            "complete_pct": pct(b["complete"], b["count"]),
        }

    overall = score(buckets.pop("__all__"))
    by_method = {m: score(buckets[m]) for m in sorted(buckets)}
    return {
        "date": lib.today(),
        "measured_at": lib.iso_now(),
        "enforcement": "prompt_only",
        "ontology_version": ontology.get("version"),
        "total_assertions": overall["count"],
        "overall": overall,
        "by_method": by_method,
    }


def same_numbers(a, b) -> bool:
    return (a.get("date") == b.get("date")
            and a.get("overall") == b.get("overall")
            and a.get("by_method") == b.get("by_method"))


def print_row(row):
    o = row["overall"]
    print(f"hygiene {row['date']}  enforcement={row['enforcement']}  "
          f"assertions={row['total_assertions']}")
    print(f"  overall           complete {o['complete_pct']}%   "
          f"source {o['with_source_pct']}%   root {o['with_resolvable_root_pct']}%   "
          f"band {o['with_band_pct']}%")
    for method in sorted(row["by_method"]):
        m = row["by_method"][method]
        print(f"  {method:<16}  complete {m['complete_pct']}%   "
              f"source {m['with_source_pct']}%   root {m['with_resolvable_root_pct']}%   "
              f"band {m['with_band_pct']}%   (n={m['count']})")


def main():
    ap = argparse.ArgumentParser(description="Measure provenance hygiene.")
    ap.add_argument("--no-append", action="store_true", help="do not touch the history file")
    ap.add_argument("--history", action="store_true", help="print the recorded series and exit")
    args = ap.parse_args()

    if args.history:
        rows = lib.load_jsonl(lib.HYGIENE_HISTORY_PATH)
        if not rows:
            print("no hygiene history yet")
            return 0
        for r in rows:
            o = r["overall"]
            print(f"{r['date']}  complete {o['complete_pct']}%  "
                  f"source {o['with_source_pct']}%  root {o['with_resolvable_root_pct']}%  "
                  f"band {o['with_band_pct']}%  n={r['total_assertions']}")
        return 0

    row = measure()
    print_row(row)

    if args.no_append:
        return 0
    history = lib.load_jsonl(lib.HYGIENE_HISTORY_PATH)
    if history and same_numbers(history[-1], row):
        print("  unchanged since the last measurement today; nothing appended")
        return 0
    lib.append_jsonl(lib.HYGIENE_HISTORY_PATH, [row])
    print(f"  appended a row to {lib.rel(lib.HYGIENE_HISTORY_PATH)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
