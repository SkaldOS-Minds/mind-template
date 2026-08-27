#!/usr/bin/env sh
# neighbors.sh: the read recipe. One hop out from a node, with titles and belief.
#
#   scripts/neighbors.sh <node-id> [--all]
#
# Shows active edges by default; --all includes archived ones. Belief status
# comes from graph/projections.jsonl, so run scripts/dream.py first if the
# graph changed.
set -eu

if [ $# -lt 1 ]; then
  echo "usage: scripts/neighbors.sh <node-id> [--all]" >&2
  exit 2
fi

NODE="$1"
MODE="${2:---active}"
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

if ! command -v jq >/dev/null 2>&1; then
  echo "neighbors.sh needs jq" >&2
  exit 3
fi

[ -f "$ROOT/graph/projections.jsonl" ] || : > "$ROOT/graph/projections.jsonl"

jq -rn \
  --arg id "$NODE" \
  --arg mode "$MODE" \
  --slurpfile nodes "$ROOT/graph/nodes.jsonl" \
  --slurpfile edges "$ROOT/graph/edges.jsonl" \
  --slurpfile proj "$ROOT/graph/projections.jsonl" '
  def nodelabel($title; $n): ($title[$n] // "(orphan)") + " [" + $n + "]";
  def belief($beliefs; $k):
    ($beliefs[$k]) as $p
    | if $p == null then "not yet dreamed"
      else $p.status + " (" + ($p.independent_support_count | tostring) + " src, support "
           + (($p.support_score // 0) | tostring) + ")" end;

  ($nodes | map({key: .id, value: .title}) | from_entries) as $title
  | ($proj | map({key: .edge_key, value: .}) | from_entries) as $beliefs
  | ($title[$id] // "(unknown node)") as $self
  | "node: " + $id + "  " + $self,
    "",
    "outgoing:",
    ( $edges[]
      | select(.src == $id)
      | select($mode == "--all" or .active)
      | "  -" + (if .active then "" else " [archived]" end) + " " + .predicate
        + " -> " + nodelabel($title; .dst) + "  band=" + (.band // "none")
        + "  belief=" + belief($beliefs; .key) ),
    "",
    "incoming:",
    ( $edges[]
      | select(.dst == $id)
      | select($mode == "--all" or .active)
      | "  -" + (if .active then "" else " [archived]" end) + " " + nodelabel($title; .src)
        + " -" + .predicate + "-> here  band=" + (.band // "none")
        + "  belief=" + belief($beliefs; .key) )
'
