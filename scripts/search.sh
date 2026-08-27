#!/usr/bin/env sh
# search.sh: the read recipe. Grep the vault and the graph for a pattern.
#
#   scripts/search.sh <pattern> [--vault|--graph|--log]
#
# Default searches the vault notes and the derived graph files. The thin-tool
# layer in the hub reimplements exactly this server-side, so what you reach for
# here is what the MCP `search` tool should do.
set -eu

if [ $# -lt 1 ]; then
  echo "usage: scripts/search.sh <pattern> [--vault|--graph|--log]" >&2
  exit 2
fi

PATTERN="$1"
SCOPE="${2:---all}"
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

if command -v rg >/dev/null 2>&1; then
  SEARCH="rg --line-number --color never --no-heading"
else
  SEARCH="grep -rn"
fi

search_in() {
  # shellcheck disable=SC2086
  $SEARCH -- "$PATTERN" "$@" 2>/dev/null || true
}

case "$SCOPE" in
  --vault)
    search_in "$ROOT/vault"
    ;;
  --graph)
    search_in "$ROOT/graph/nodes.jsonl" "$ROOT/graph/edges.jsonl" "$ROOT/graph/projections.jsonl"
    ;;
  --log)
    search_in "$ROOT/graph/assertions.jsonl"
    ;;
  *)
    echo "== vault =="
    search_in "$ROOT/vault"
    echo
    echo "== graph (derived) =="
    search_in "$ROOT/graph/nodes.jsonl" "$ROOT/graph/edges.jsonl" "$ROOT/graph/projections.jsonl"
    echo
    echo "== assertion log =="
    search_in "$ROOT/graph/assertions.jsonl"
    ;;
esac
