"""Shared helpers for the SkaldOS Mind rig scripts. Stdlib only."""
import json
import os
import re
import datetime as _dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRAPH = ROOT / "graph"
VAULT = ROOT / "vault"
ONTOLOGY_PATH = ROOT / "ontology" / "ontology.json"

ASSERTIONS_PATH = GRAPH / "assertions.jsonl"
SOURCES_PATH = GRAPH / "sources.jsonl"
NODES_PATH = GRAPH / "nodes.jsonl"
EDGES_PATH = GRAPH / "edges.jsonl"
PROJECTIONS_PATH = GRAPH / "projections.jsonl"
HYGIENE_HISTORY_PATH = GRAPH / "hygiene-history.jsonl"
DREAM_HISTORY_PATH = GRAPH / "dream-history.jsonl"

ASSERTION_KINDS = ("node_upsert", "edge_assert", "edge_archive", "tag_assign")
METHODS = ("human", "agent", "ingest")

# The SkaldOS Edda Standard (design 4.2). The first line of every Edda log is a
# header record, {"edda": "0", "mind": "<slug>"}, so a log self-identifies when
# it travels between hosts.
EDDA_VERSION = "0"
DEFAULT_MIND_SLUG = "unnamed"

# Every field the rig knows about. promote.py reports anything outside this set
# as unmapped rather than silently dropping it.
KNOWN_ASSERTION_FIELDS = (
    "id", "kind", "node", "edge", "node_id", "tag", "value", "reason",
    "confidence_band", "source_identifier", "derived_from", "method", "by",
    "asserted_at",
)


def load_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path: Path, rows):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def render_jsonl(rows) -> str:
    """The exact bytes write_jsonl would produce. Used for drift comparison."""
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


def append_jsonl(path: Path, rows):
    """The only sanctioned write to an append-only file."""
    with path.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def load_ontology():
    return json.loads(ONTOLOGY_PATH.read_text())


def edge_key(edge: dict) -> str:
    return f"{edge['src']}|{edge['predicate']}|{edge['dst']}"


def load_sources() -> dict:
    return {s["id"]: s for s in load_jsonl(SOURCES_PATH)}


def split_edda_log(path: Path = ASSERTIONS_PATH):
    """(header record or None, assertion rows). The header is not an assertion
    and never reaches the fold, the dream or the hygiene count."""
    rows = load_jsonl(path)
    if rows and isinstance(rows[0], dict) and "edda" in rows[0]:
        return rows[0], rows[1:]
    return None, rows


def edda_header(path: Path = ASSERTIONS_PATH):
    return split_edda_log(path)[0]


def load_assertions() -> list:
    return split_edda_log()[1]


def mind_slug(path: Path = ASSERTIONS_PATH) -> str:
    """This mind's slug, resolved without any config file.

    A mind stamped from the template starts with an empty log, so the slug has
    to come from somewhere. In order: the header already written (a mind names
    itself once and never renames), the MIND_SLUG environment variable, and
    finally the repository directory name with any `mind-` prefix stripped.
    Instantiating the template as `mind-wiersholm` therefore yields `wiersholm`
    on the first write, with no editing step.
    """
    header = edda_header(path)
    if header and header.get("mind"):
        return header["mind"]
    env = (os.environ.get("MIND_SLUG") or "").strip()
    if env:
        return _SLUG_RE.sub("-", env.lower()).strip("-") or DEFAULT_MIND_SLUG
    name = ROOT.name
    if name.startswith("mind-"):
        name = name[len("mind-"):]
    return _SLUG_RE.sub("-", name.lower()).strip("-") or DEFAULT_MIND_SLUG


def ensure_edda_header(slug: str | None = None, path: Path = ASSERTIONS_PATH) -> bool:
    """Write the header if the log has none. Returns True when it wrote one.

    On a log that predates the standard this inserts one line at the top and
    touches no assertion row, so `git diff` shows a single addition. On a new or
    empty log it is simply the first line written. Writing the header of a log
    that has none is initialisation, not mutation: no assertion row is touched,
    and it happens exactly once in a mind's life.
    """
    if slug is None:
        slug = mind_slug(path)
    header = {"edda": EDDA_VERSION, "mind": slug}
    if not path.exists() or not path.read_text().strip():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(header, ensure_ascii=False) + "\n")
        return True
    if edda_header(path) is not None:
        return False
    body = path.read_text()
    path.write_text(json.dumps(header, ensure_ascii=False) + "\n" + body)
    return True


def index_by_id(assertions) -> dict:
    return {a["id"]: a for a in assertions if "id" in a}


def resolve_root_source(assertion: dict, by_id: dict, _seen=None) -> str | None:
    """Walk source_identifier / derived_from lineage to a root source id, or None."""
    if _seen is None:
        _seen = set()
    if assertion["id"] in _seen:
        return None  # cycle guard
    _seen.add(assertion["id"])
    if assertion.get("source_identifier"):
        return assertion["source_identifier"]
    parent = assertion.get("derived_from")
    if not parent:
        return None
    if parent.startswith("src:"):
        return parent
    parent_assertion = by_id.get(parent)
    if parent_assertion is None:
        return None
    return resolve_root_source(parent_assertion, by_id, _seen)


def band_weight(ontology: dict, band) -> float | None:
    if not band:
        return None
    return ontology.get("confidence_bands", {}).get(band)


def predicate_kind(ontology: dict, predicate: str) -> str | None:
    spec = ontology.get("predicates", {}).get(predicate)
    return spec.get("kind") if spec else None


def dream_config(ontology: dict) -> dict:
    """Method config lives in the ontology (design 4.3). Defaults keep old files working."""
    cfg = dict(ontology.get("dream_method", {}))
    cfg.setdefault("method", "evidence_weighted")
    cfg.setdefault("version", "v0")
    cfg.setdefault("support_threshold", 0.7)
    cfg.setdefault("contest_support_floor", 0.3)
    cfg.setdefault("contest_counter_floor", 0.3)
    cfg.setdefault("min_independent_sources", 2)
    cfg.setdefault("unbanded_weight", 0.2)
    return cfg


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(node_id: str) -> str:
    base = node_id.split(":", 1)[-1].lower()
    slug = _SLUG_RE.sub("-", base).strip("-")
    return slug or "node"


def next_assertion_id(assertions) -> str:
    highest = 0
    for a in assertions:
        aid = a.get("id", "")
        m = re.fullmatch(r"a-(\d+)", aid or "")
        if m:
            highest = max(highest, int(m.group(1)))
    return f"a-{highest + 1:04d}"


def iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).date().isoformat()


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)
