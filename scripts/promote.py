#!/usr/bin/env python3
"""promote.py: replay this mind's assertion log into a SkaldOS tenant.

`--dry-run` streams the Edda log (graph/assertions.jsonl, edda/0) through the
field mapping in design section 10.2 and prints the assert-IR batches that would
be POSTed to /graph/assert, plus a report of anything the mapping does not
carry.

`--target` adds the live half (P1.5). It does not replay the log: it takes the
distinct claim SHAPES the log contains, sends one representative of each to the
real endpoint, and records the verdict. That is what answers the questions a
local dry run cannot ("does the endpoint accept a sourceless claim", "does a tag
take a list"), at the cost of a handful of writes rather than a whole mind's
worth. A full replay waits until the mapping below actually matches the
endpoint's contract; the 2026-08-27 probe run recorded in
docs/promote-dryrun-notes.md shows it does not yet.

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

Idempotency. Keys are per claim and content-derived:

    <replay id>-<assertion id>-<12 hex of the claim digest>

The replay id is a fixed function of the log itself (edda version and mind
slug), never of how the run happened to slice batches. An earlier version keyed
batches by their first and last assertion id, so a retry after the log grew
produced different keys and re-wrote everything the first attempt had already
landed. Batch boundaries are a transport detail; an idempotency key must not be
able to see them.

Usage:
    python3 scripts/promote.py --dry-run
    python3 scripts/promote.py --dry-run --batch-size 25 --out /tmp/batches.json
    python3 scripts/promote.py --dry-run --summary
    python3 scripts/promote.py --dry-run \\
        --target https://api.staging.skaldos.ai/graph/assert \\
        --token-env SKALDOS_STAGING_TOKEN \\
        --context '{"tenant": "...", "classification": "C1", "vertical": "foundation",
                    "ontology_version": "...", "valid_from": "2026-08-27T08:00:00Z",
                    "valid_to": null}' \\
        --actor-principal <uuid> --probe-report /tmp/probes.json

The token is read from the named environment variable and never from the
command line, so it stays out of shell history and process listings.
"""
import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
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


def replay_id(header) -> str:
    """A fixed handle for "this log, replayed". Depends on the log's identity
    only, so every attempt at the same replay produces the same keys."""
    return f"mind-replay-edda{header.get('edda', lib.EDDA_VERSION)}-{header.get('mind', lib.DEFAULT_MIND_SLUG)}"


def claim_digest(claim) -> str:
    """Stable digest of a claim's content, boundary-free by construction."""
    canonical = json.dumps(claim, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


def build_batches(assertions, batch_size, replay):
    symbols = symbol_map(assertions)
    unmapped = []
    claims = []
    for a in assertions:
        claim = map_assertion(a, symbols, unmapped)
        # Per claim, content-derived, and blind to batch boundaries. See the
        # module docstring: keying on the slice was a real idempotency defect.
        claim["idempotency_key"] = (
            f"{replay}-{claim['mind_assertion_id']}-{claim_digest(claim)[:12]}")
        claims.append(claim)
    batches = []
    for i in range(0, len(claims), batch_size):
        chunk = claims[i:i + batch_size]
        batches.append({
            "batch": len(batches) + 1,
            "source_format": f"edda/{lib.EDDA_VERSION}",
            "endpoint": "POST /graph/assert",
            "replay_id": replay,
            "claims": chunk,
        })
    return batches, unmapped


# ---------------------------------------------------------------------------
# The live half: one probe per claim shape (P1.5)
# ---------------------------------------------------------------------------

def shape_of(a) -> str:
    """The signature that decides whether two assertions ask the endpoint the
    same question. One probe per shape covers the whole log."""
    kind = a.get("kind")
    parts = [kind or "unknown"]
    parts.append("source" if a.get("source_identifier") else "sourceless")
    derived = a.get("derived_from")
    if not derived:
        parts.append("lineage:none")
    elif derived.startswith("src:"):
        parts.append("lineage:source")
    else:
        parts.append("lineage:claim")
    if kind == "node_upsert":
        tags = (a.get("node") or {}).get("tags") or {}
        parts.append("tags:list" if any(isinstance(v, list) for v in tags.values())
                     else ("tags:scalar" if tags else "tags:none"))
    if kind == "tag_assign":
        parts.append("value:list" if isinstance(a.get("value"), list) else "value:scalar")
    if kind == "edge_archive":
        parts.append("reason" if a.get("reason") else "no-reason")
    return "|".join(parts)


def shape_index(assertions):
    """shape -> (representative assertion, member ids). Ordered as the log is."""
    index = {}
    for a in assertions:
        s = shape_of(a)
        if s not in index:
            index[s] = {"representative": a, "members": []}
        index[s]["members"].append(a.get("id"))
    return index


def probe_request(claim, context, actor_principal, source_kind):
    """The envelope this mapping believes /graph/assert takes. Sending it is the
    whole point: the endpoint's answer is the finding."""
    body = dict(claim)
    if context:
        body["context"] = context
    envelope = {
        "claim": body,
        "actor": {"principal_id": actor_principal, "kind": "agent"} if actor_principal else None,
        "source": {"kind": source_kind,
                   "identifier": (claim.get("source") or {}).get("identifier")}
        if claim.get("source") else {"kind": source_kind},
        "grounding": [],
        "intent": "assertive",
    }
    if envelope["actor"] is None:
        del envelope["actor"]
    return envelope


def post_json(url, payload, token, idempotency_key, timeout=40):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "Idempotency-Key": idempotency_key,
    }
    if token:
        headers["authorization"] = "Bearer " + token
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # network, DNS, TLS: still a verdict worth printing
        return None, str(exc)


def verdict(status, body_text):
    """A live driver that trusts the HTTP status alone is wrong here: the
    endpoint answers 202 and then says `"status": "rejected"` in the body when
    it staged a claim it could not resolve. Accepted means both."""
    if status is None:
        return "unreachable", None
    try:
        body = json.loads(body_text)
    except ValueError:
        return ("accepted" if 200 <= status < 300 else "rejected"), None
    reason = None
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, dict):
            reason = detail.get("reason_code") or detail.get("error_code")
        elif isinstance(detail, list) and detail:
            first = detail[0]
            if isinstance(first, dict):
                reason = f"{first.get('type')} at {'.'.join(str(p) for p in first.get('loc', []))}"
        elif body.get("status"):
            reason = body.get("status")
    if 200 <= status < 300:
        if isinstance(body, dict) and body.get("status") in ("rejected", "conflict"):
            return "rejected", reason
        return "accepted", reason
    return "rejected", reason


def run_probes(args, header, assertions, batches):
    token = os.environ.get(args.token_env) if args.token_env else None
    if args.token_env and not token:
        print(f"promote.py: ${args.token_env} is empty; probing unauthenticated",
              file=sys.stderr)
    context = json.loads(args.context) if args.context else None
    by_key = {c["mind_assertion_id"]: c for b in batches for c in b["claims"]}

    results = []
    for shape, entry in shape_index(assertions).items():
        if args.probe_limit and len(results) >= args.probe_limit:
            results.append({"shape": shape, "verdict": "not probed",
                            "members": len(entry["members"]),
                            "note": f"--probe-limit {args.probe_limit} reached"})
            continue
        rep = entry["representative"]
        claim = by_key[rep["id"]]
        payload = probe_request(claim, context, args.actor_principal, args.source_kind)
        status, body_text = post_json(args.target, payload, token, claim["idempotency_key"])
        outcome, reason = verdict(status, body_text)
        results.append({
            "shape": shape,
            "representative": rep["id"],
            "members": len(entry["members"]),
            "http_status": status,
            "verdict": outcome,
            "reason": reason,
            "response": body_text[:600],
        })
    return results


def main():
    ap = argparse.ArgumentParser(description="Replay the assertion log into a tenant.")
    ap.add_argument("--dry-run", action="store_true", required=True,
                    help="print the batches rather than replaying the log")
    ap.add_argument("--batch-size", type=int, default=50)
    ap.add_argument("--target",
                    help="live /graph/assert URL; sends one probe per claim shape "
                         "and reports the endpoint's verdict")
    ap.add_argument("--token-env", default="SKALDOS_ASSERT_TOKEN",
                    help="environment variable holding the bearer token for --target "
                         "(never pass the token itself on the command line)")
    ap.add_argument("--context",
                    help="JSON context-of-validity merged into every probe claim "
                         "(tenant, classification, vertical, ontology_version, "
                         "valid_from, valid_to)")
    ap.add_argument("--actor-principal", help="principal id (uuid) for --target probes")
    ap.add_argument("--source-kind", default="ingest",
                    help="AssertionSource.kind for --target probes; the endpoint's "
                         "pattern is ^[a-z][a-z0-9-]{0,62}$, which this mind's own "
                         "source kinds do not satisfy")
    ap.add_argument("--probe-limit", type=int, default=0,
                    help="stop after N live probes (0 = one per shape)")
    ap.add_argument("--probe-report", help="write the probe results to a JSON file")
    ap.add_argument("--out", help="write the batches to a file instead of stdout")
    ap.add_argument("--summary", action="store_true", help="counts and gaps only")
    args = ap.parse_args()

    header, assertions = lib.split_edda_log()
    ontology = lib.load_ontology()
    if header is None:
        print("promote.py: the log has no Edda header record; run scripts/check.py",
              file=sys.stderr)
        return 1
    replay = replay_id(header)
    batches, unmapped = build_batches(assertions, args.batch_size, replay)

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
    print(f"  replay id: {replay} (idempotency keys are per claim and content-derived)",
          file=sys.stderr)
    print("  does not carry:", file=sys.stderr)
    for item in NOT_CARRIED:
        print(f"    - {item}", file=sys.stderr)

    exit_code = 0
    if unmapped:
        print(f"  unmapped fields ({len(unmapped)}):", file=sys.stderr)
        for u in unmapped:
            print(f"    - {u}", file=sys.stderr)
        exit_code = 1
    else:
        print("  unmapped fields: none", file=sys.stderr)

    if args.target:
        results = run_probes(args, header, assertions, batches)
        if args.probe_report:
            Path(args.probe_report).write_text(
                json.dumps(results, ensure_ascii=False, indent=2) + "\n")
        print(f"\n  live probe against {args.target}", file=sys.stderr)
        probed = [r for r in results if r["verdict"] != "not probed"]
        skipped = len(results) - len(probed)
        accepted = 0
        for r in results:
            print(f"    {r['shape']}", file=sys.stderr)
            print(f"      {r.get('representative', '-')} covers {r['members']} assertion(s): "
                  f"HTTP {r.get('http_status')} -> {r['verdict']}"
                  f"{' (' + str(r['reason']) + ')' if r.get('reason') else ''}",
                  file=sys.stderr)
            if r["verdict"] == "accepted":
                accepted += 1
        print(f"    {accepted}/{len(probed)} probed shape(s) accepted by the live endpoint"
              f"{f', {skipped} not probed' if skipped else ''}", file=sys.stderr)
        if accepted < len(probed):
            print("    the mapping does not yet match the endpoint's contract; see "
                  "docs/promote-dryrun-notes.md", file=sys.stderr)
            exit_code = 1
        elif skipped:
            print("    --probe-limit left shapes unprobed; this is not a clean run",
                  file=sys.stderr)
            exit_code = 1
    else:
        print("  target: not set (pass --target to probe a live /graph/assert)",
              file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
