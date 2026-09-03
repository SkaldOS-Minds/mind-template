---
name: hygiene-review
budget:
  usd: 1.50
  usd_per_step: 0.25
  steps: 40
  wall_clock_minutes: 20
tools: [mind_search, mind_node, mind_neighbors, mind_dream, read, ls, grep]
---

Read `graph/hygiene-history.jsonl` and report on how this mind's hygiene has
moved since the last ten measurements.

Say which of the three counted gaps moved — assertions with no resolvable
lineage root, assertions with no confidence band, isolated nodes — and for each
one that grew, name the specific assertion ids or node ids behind the growth.
Use `mind_search` and `mind_neighbors` to check whether an isolated node is
genuinely unconnected or merely waiting on an edge someone meant to assert.

Do not assert anything. This run reports; a person decides what to claim. If
the report needs a claim to be useful, say the claim you would make and the
source you would cite for it, and stop there.

Ship a plain-text report. No file writes, no commits.
