#!/usr/bin/env python3
"""project.py: deterministic fold from the assertion log to every derived file.

Inputs:  graph/assertions.jsonl (source of truth), ontology/ontology.json,
         graph/projections.jsonl (optional, for edge-block status), vault bodies.
Outputs: graph/nodes.jsonl, graph/edges.jsonl, vault/*.md

Machine-owned regions of a vault note: the YAML frontmatter, the title heading
and the edges block. The body region between the body markers is human-owned and
is copied through byte for byte. Running this script twice produces zero diff.

Usage:
    python3 scripts/project.py            # write derived files
    python3 scripts/project.py --check    # report drift, write nothing (exit 1 on drift)
    python3 scripts/project.py --quiet
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

BODY_START = "<!-- mind:body:start -->"
BODY_END = "<!-- mind:body:end -->"
EDGES_START = "<!-- mind:edges:start -->"
EDGES_END = "<!-- mind:edges:end -->"
DEFAULT_BODY = (
    "\n_Human-owned region. Write freely here; project.py copies it through byte for byte._\n\n"
)


# --------------------------------------------------------------------------
# the fold
# --------------------------------------------------------------------------

def apply_tags(node, tags_payload, ontology, problems, assertion_id):
    for name in sorted(tags_payload):
        value = tags_payload[name]
        spec = ontology.get("tags", {}).get(name)
        values = value if isinstance(value, list) else [value]
        if spec is None:
            problems.append(f"{assertion_id}: unknown tag '{name}'")
            node["tags"][name] = values if len(values) > 1 else values[0]
            continue
        closed = spec.get("closed_set")
        for v in values:
            if closed is not None and v not in closed:
                problems.append(
                    f"{assertion_id}: tag '{name}' value '{v}' outside closed set {closed}"
                )
        if spec.get("cardinality") == "single":
            if len(values) != 1:
                problems.append(
                    f"{assertion_id}: tag '{name}' is single-cardinality, got {len(values)} values"
                )
            node["tags"][name] = values[-1]
        else:
            current = node["tags"].setdefault(name, [])
            if not isinstance(current, list):
                current = [current]
                node["tags"][name] = current
            for v in values:
                if v not in current:
                    current.append(v)


def fold(assertions, ontology):
    """Replay the log. Returns (nodes, edges, problems). Permissive: records
    every structural problem it sees but never drops a row, so check.py can
    report on a log that was written around assert.py."""
    nodes: dict = {}
    edges: dict = {}
    problems: list = []
    node_types = ontology.get("node_types", {})
    predicates = ontology.get("predicates", {})

    for a in assertions:
        aid = a.get("id", "<no id>")
        kind = a.get("kind")
        at = a.get("asserted_at")

        if kind == "node_upsert":
            payload = a.get("node") or {}
            nid = payload.get("id")
            if not nid:
                problems.append(f"{aid}: node_upsert without node.id")
                continue
            node = nodes.get(nid)
            if node is None:
                node = {
                    "id": nid,
                    "type": payload.get("type"),
                    "title": payload.get("title") or nid,
                    "tags": {},
                    "created_at": at,
                    "updated_at": at,
                    "assertions": [],
                }
                nodes[nid] = node
            if payload.get("type"):
                node["type"] = payload["type"]
            if payload.get("title"):
                node["title"] = payload["title"]
            apply_tags(node, payload.get("tags") or {}, ontology, problems, aid)
            node["updated_at"] = at
            node["assertions"].append(aid)
            if node["type"] not in node_types:
                problems.append(f"{aid}: unknown node type '{node['type']}' for {nid}")

        elif kind == "tag_assign":
            nid = a.get("node_id")
            if not nid:
                problems.append(f"{aid}: tag_assign without node_id")
                continue
            node = nodes.get(nid)
            if node is None:
                problems.append(f"{aid}: tag_assign targets unknown node '{nid}'")
                node = {
                    "id": nid, "type": None, "title": nid, "tags": {},
                    "created_at": at, "updated_at": at, "assertions": [],
                }
                nodes[nid] = node
            apply_tags(node, {a.get("tag"): a.get("value")}, ontology, problems, aid)
            node["updated_at"] = at
            node["assertions"].append(aid)

        elif kind in ("edge_assert", "edge_archive"):
            payload = a.get("edge") or {}
            if not all(payload.get(k) for k in ("src", "predicate", "dst")):
                problems.append(f"{aid}: {kind} without a complete edge")
                continue
            key = lib.edge_key(payload)
            edge = edges.get(key)
            if edge is None:
                edge = {
                    "key": key,
                    "src": payload["src"],
                    "predicate": payload["predicate"],
                    "dst": payload["dst"],
                    "active": False,
                    "band": None,
                    "asserts": [],
                    "archives": [],
                    "first_asserted_at": at,
                    "last_event_at": at,
                    "last_reason": None,
                }
                edges[key] = edge
            if payload["predicate"] not in predicates:
                problems.append(f"{aid}: unknown predicate '{payload['predicate']}'")
            if kind == "edge_assert":
                edge["active"] = True
                edge["asserts"].append(aid)
                edge["band"] = a.get("confidence_band")
                edge["last_reason"] = None
            else:
                if not edge["asserts"]:
                    problems.append(f"{aid}: edge_archive with no prior edge_assert for {key}")
                edge["active"] = False
                edge["archives"].append(aid)
                edge["last_reason"] = a.get("reason")
            edge["last_event_at"] = at

        else:
            problems.append(f"{aid}: unknown assertion kind '{kind}'")

    # endpoint type contracts, evaluated once the whole log is folded
    for key in sorted(edges):
        edge = edges[key]
        spec = predicates.get(edge["predicate"])
        if not spec:
            continue
        for side, field in (("src", "src"), ("dst", "dst")):
            allowed = spec.get(side)
            if not allowed:
                continue
            endpoint = nodes.get(edge[field])
            if endpoint is None:
                continue  # orphan; check.py reports it separately
            if endpoint.get("type") not in allowed:
                problems.append(
                    f"{key}: {side} node '{edge[field]}' is type "
                    f"'{endpoint.get('type')}', predicate requires {allowed}"
                )
    return nodes, edges, problems


def node_rows(nodes):
    rows = []
    for nid in sorted(nodes):
        n = nodes[nid]
        tags = {k: n["tags"][k] for k in sorted(n["tags"])}
        rows.append({
            "id": n["id"],
            "type": n["type"],
            "title": n["title"],
            "tags": tags,
            "created_at": n["created_at"],
            "updated_at": n["updated_at"],
            "assertion_count": len(n["assertions"]),
            "assertions": list(n["assertions"]),
        })
    return rows


def edge_rows(edges, ontology):
    rows = []
    for key in sorted(edges, key=lambda k: (edges[k]["src"], edges[k]["predicate"], edges[k]["dst"])):
        e = edges[key]
        rows.append({
            "key": e["key"],
            "src": e["src"],
            "predicate": e["predicate"],
            "dst": e["dst"],
            "predicate_kind": lib.predicate_kind(ontology, e["predicate"]),
            "active": e["active"],
            "band": e["band"],
            "assert_count": len(e["asserts"]),
            "archive_count": len(e["archives"]),
            "asserts": list(e["asserts"]),
            "archives": list(e["archives"]),
            "first_asserted_at": e["first_asserted_at"],
            "last_event_at": e["last_event_at"],
            "last_archive_reason": e["last_reason"],
        })
    return rows


# --------------------------------------------------------------------------
# vault rendering
# --------------------------------------------------------------------------

def vault_slugs(nodes) -> dict:
    """Deterministic node id -> vault filename stem. Lowest node id wins a
    contested slug; later collisions get a stable suffix."""
    taken = {}
    slugs = {}
    for nid in sorted(nodes):
        base = lib.slugify(nid)
        slug = base
        n = 2
        while slug in taken:
            slug = f"{base}-{n}"
            n += 1
        taken[slug] = nid
        slugs[nid] = slug
    return slugs


def extract_body(text: str | None) -> str:
    """Pull the human-owned region out of an existing note, byte for byte."""
    if not text:
        return DEFAULT_BODY
    start = text.find(BODY_START)
    end = text.find(BODY_END)
    if start != -1 and end != -1 and end > start:
        body = text[start + len(BODY_START):end]
        if body.startswith("\n"):
            body = body[1:]
        if body and not body.endswith("\n"):
            body += "\n"
        return body
    # A note written by hand without markers: adopt everything after the
    # frontmatter (minus any edges block) as the body, once.
    rest = text
    if rest.startswith("---\n"):
        closing = rest.find("\n---\n", 3)
        if closing != -1:
            rest = rest[closing + len("\n---\n"):]
    es = rest.find(EDGES_START)
    if es != -1:
        rest = rest[:es]
    rest = rest.strip("\n")
    lines = [ln for ln in rest.split("\n")]
    while lines and lines[0].startswith("# "):
        lines = lines[1:]
    rest = "\n".join(lines).strip("\n")
    return f"\n{rest}\n\n" if rest else DEFAULT_BODY


def yaml_scalar(value) -> str:
    import json as _json
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return _json.dumps(value)
    return _json.dumps(str(value), ensure_ascii=False)


def frontmatter(node, slug) -> str:
    out = ["---"]
    out.append(f"id: {yaml_scalar(node['id'])}")
    out.append(f"type: {yaml_scalar(node['type'])}")
    out.append(f"title: {yaml_scalar(node['title'])}")
    tags = node["tags"]
    if not tags:
        out.append("tags: {}")
    else:
        out.append("tags:")
        for name in sorted(tags):
            value = tags[name]
            if isinstance(value, list):
                out.append(f"  {name}:")
                for v in value:
                    out.append(f"    - {yaml_scalar(v)}")
            else:
                out.append(f"  {name}: {yaml_scalar(value)}")
    out.append(f"created_at: {yaml_scalar(node['created_at'])}")
    out.append(f"updated_at: {yaml_scalar(node['updated_at'])}")
    out.append(f"assertion_count: {node['assertion_count']}")
    out.append(f"slug: {yaml_scalar(slug)}")
    out.append('generated_by: "project.py"')
    out.append('source_of_truth: "graph/assertions.jsonl"')
    out.append("---")
    return "\n".join(out) + "\n"


def status_suffix(projection) -> str:
    if projection is None:
        return "status: not yet dreamed"
    count = projection.get("independent_support_count", 0)
    word = "source" if count == 1 else "sources"
    return f"status: {projection['status']} ({count} independent {word})"


def edges_block(node, edges_by_node, node_rows_by_id, slugs, projections) -> str:
    nid = node["id"]
    out = [EDGES_START, "## Edges", ""]
    outgoing = edges_by_node.get(nid, {}).get("out", [])
    incoming = edges_by_node.get(nid, {}).get("in", [])
    active_out = [e for e in outgoing if e["active"]]
    active_in = [e for e in incoming if e["active"]]

    out.append("### Outgoing")
    if not active_out:
        out.append("_None._")
    for e in active_out:
        target = node_rows_by_id.get(e["dst"])
        link = (
            f"[[{slugs[e['dst']]}|{target['title']}]]" if target else f"`{e['dst']}` (orphan)"
        )
        out.append(
            f"- `{e['predicate']}` -> {link} | band: {e['band'] or 'unbanded'} | "
            f"{status_suffix(projections.get(e['key']))}"
        )
    out.append("")
    out.append("### Incoming")
    if not active_in:
        out.append("_None._")
    for e in active_in:
        origin = node_rows_by_id.get(e["src"])
        link = (
            f"[[{slugs[e['src']]}|{origin['title']}]]" if origin else f"`{e['src']}` (orphan)"
        )
        out.append(
            f"- {link} -`{e['predicate']}`-> this note | band: {e['band'] or 'unbanded'} | "
            f"{status_suffix(projections.get(e['key']))}"
        )
    archived = [e for e in outgoing + incoming if not e["active"]]
    if archived:
        out.append("")
        out.append("### Archived")
        for e in archived:
            reason = e.get("last_archive_reason") or "no reason recorded"
            out.append(f"- `{e['src']}` -`{e['predicate']}`-> `{e['dst']}` | archived: {reason}")
    out.append(EDGES_END)
    return "\n".join(out) + "\n"


def render_vault(nrows, erows, projections, existing: dict) -> dict:
    """Return {relative path: full note text}. `existing` maps the same relative
    paths to the current on-disk text, so human bodies survive."""
    slugs = vault_slugs({n["id"]: n for n in nrows})
    by_id = {n["id"]: n for n in nrows}
    by_node: dict = {}
    for e in erows:
        by_node.setdefault(e["src"], {}).setdefault("out", []).append(e)
        by_node.setdefault(e["dst"], {}).setdefault("in", []).append(e)
    proj_by_key = {p["edge_key"]: p for p in projections}

    notes = {}
    for n in nrows:
        relpath = f"vault/{slugs[n['id']]}.md"
        body = extract_body(existing.get(relpath))
        text = (
            frontmatter(n, slugs[n["id"]])
            + "\n"
            + f"# {n['title']}\n"
            + "\n"
            + BODY_START + "\n"
            + body
            + BODY_END + "\n"
            + "\n"
            + edges_block(n, by_node, by_id, slugs, proj_by_key)
        )
        notes[relpath] = text
    return notes


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def read_existing_vault() -> dict:
    out = {}
    if lib.VAULT.exists():
        for p in sorted(lib.VAULT.rglob("*.md")):
            out[lib.rel(p)] = p.read_text()
    return out


def compute():
    """Everything project.py would write, as text, without touching disk."""
    ontology = lib.load_ontology()
    assertions = lib.load_assertions()
    nodes, edges, problems = fold(assertions, ontology)
    nrows = node_rows(nodes)
    erows = edge_rows(edges, ontology)
    projections = lib.load_jsonl(lib.PROJECTIONS_PATH)
    notes = render_vault(nrows, erows, projections, read_existing_vault())
    files = {
        lib.rel(lib.NODES_PATH): lib.render_jsonl(nrows),
        lib.rel(lib.EDGES_PATH): lib.render_jsonl(erows),
    }
    files.update(notes)
    return {
        "files": files,
        "nodes": nrows,
        "edges": erows,
        "problems": problems,
        "assertion_count": len(assertions),
    }


def drift(result) -> list:
    """Relative paths whose machine-owned content differs from disk, plus
    vault notes on disk that the log does not account for."""
    out = []
    for relpath, text in sorted(result["files"].items()):
        path = lib.ROOT / relpath
        current = path.read_text() if path.exists() else None
        if current != text:
            out.append(relpath)
    for relpath in read_existing_vault():
        if relpath not in result["files"]:
            out.append(f"{relpath} (no node in the log)")
    return out


def write(result, quiet=False):
    lib.VAULT.mkdir(parents=True, exist_ok=True)
    changed = []
    for relpath, text in sorted(result["files"].items()):
        path = lib.ROOT / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text() != text:
            path.write_text(text)
            changed.append(relpath)
    if not quiet:
        print(
            f"project.py: folded {result['assertion_count']} assertions -> "
            f"{len(result['nodes'])} nodes, {len(result['edges'])} edges, "
            f"{len(result['files']) - 2} vault notes"
        )
        if changed:
            for c in changed:
                print(f"  wrote {c}")
        else:
            print("  no changes (already projected)")
        for p in result["problems"]:
            print(f"  problem: {p}")
    return changed


def main():
    ap = argparse.ArgumentParser(description="Fold the assertion log into derived files.")
    ap.add_argument("--check", action="store_true", help="report drift, write nothing")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    # A mind stamped from the template starts with an empty log and no header.
    # The fold deliberately does not write one. A mind names itself when it
    # first speaks, through assert.py, on a machine somebody chose; if the fold
    # named it, the first CI run on the template repo itself would bake its own
    # slug into every mind ever stamped from it. That is not hypothetical: it is
    # what happened on 2026-08-27, before this comment existed.
    header, rows = lib.split_edda_log()
    if not args.quiet and header is None and not rows:
        print("project.py: this mind has no assertions yet and is not named; "
              "the first scripts/assert.py write names it")

    result = compute()
    if args.check:
        d = drift(result)
        if d:
            print("project.py --check: derived files drift from the log:")
            for item in d:
                print(f"  {item}")
            return 1
        if not args.quiet:
            print("project.py --check: derived files match the log")
        return 0
    write(result, quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
