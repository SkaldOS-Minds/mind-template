# Operating contract for this mind

You are working inside a **mind**: one git repository holding one append-only
assertion log and a set of derived projections. This file is the contract. There
is no service below you enforcing it, so read it before you write anything.

Everything here is advisory by construction. That is deliberate, and it is being
measured: `scripts/hygiene.py` records how well this contract holds. Following it
carefully is the experiment working. Working around it is data too, but the
honest kind is the kind you record.

---

## 0. If this mind is empty

A mind stamped from the SkaldOS Mind template starts with no assertions at all.
Two things are worth doing before the first claim:

1. **Know how this mind gets its name.** The first `scripts/assert.py` write
   writes the Edda header record that makes the log self-identifying, and takes
   the slug from the repository directory name with any `mind-` prefix stripped:
   a clone in `mind-wiersholm` becomes the mind `wiersholm`. Export `MIND_SLUG`
   before that first write if you want a different name. A mind names itself
   once and never renames, so this is worth thirty seconds of attention now.
   Nothing else writes the header, the fold included: a name should come from
   whoever is making the first claim, not from a robot folding an empty log.
2. **Fit the ontology to the subject.** `ontology/ontology.json` ships a
   deliberately generic starting set: seven node types, ten predicates, two
   tags. It is meant to be edited on day one, before there is data shaped by the
   wrong vocabulary. Adding a node type or a predicate is normal. Deleting one
   that assertions already use is not: `check.py` will report every assertion
   left stranded.

Registering real sources in `graph/sources.jsonl` with honest reliability priors
comes next, and it is the single highest-leverage thing you can do here. Priors
are the dial the whole epistemic layer turns on.

## 1. The one rule

**`graph/assertions.jsonl` is append-only and is the single source of truth.**

It is an **Edda log**: the SkaldOS Edda Standard, version `edda/0`. Its first
line is a header record, `{"edda": "0", "mind": "..."}`, so the file identifies
itself wherever it travels. The header is not an assertion. Everything after it
is, one per line, forever.

- Never edit a line in it. Never reorder it. Never delete from it. Never rewrite
  it with a script.
- Never hand-edit `graph/nodes.jsonl`, `graph/edges.jsonl`,
  `graph/projections.jsonl`, or the machine-owned regions of any vault note.
  They are rebuilt from the log and your edits will vanish on the next fold.
- If something in the graph is wrong, you fix it by appending a new assertion:
  a corrected `node_upsert`, or an `edge_archive` with a reason. The mind keeps
  the record of having been wrong. That record is the product.

Everything else in the repo is disposable. Delete every derived file and run
`python3 scripts/project.py && python3 scripts/dream.py` and you get the same
bytes back.

## 2. The write path

**All writes go through `scripts/assert.py`. There is no second write path.**

```sh
# a node
scripts/assert.py --band certain --source src:operator --method human --by <you> \
  node --id n:some-thing --type concept --title "Some thing" --tag domain=strategy

# an edge
scripts/assert.py --band likely --source src:operator --method agent --by my-session \
  edge --src n:some-thing --predicate supports --dst n:some-other-thing

# retract an edge (the only way to unsay one)
scripts/assert.py --band likely --source src:operator --method human --by <you> \
  archive --src n:a --predicate supports --dst n:b --reason "the article does not actually argue this"

# a tag
scripts/assert.py --band certain --source src:operator --method human --by <you> \
  tag --node n:some-thing --tag status=active

# many rows at once, as assertion IR
scripts/assert.py batch --file /tmp/rows.jsonl
```

Add `--dry-run` to any of these to see the exact row that would be appended
without appending it. Use it when you are unsure.

`assert.py` assigns the `id` and fixes `asserted_at` at write time. Never supply
either yourself: replays stay idempotent only because `asserted_at` is the
moment of the claim, not the moment of the last recompute.

After a successful write it re-folds `nodes.jsonl`, `edges.jsonl` and the vault.
It does not re-dream. Run `scripts/dream.py` when you want the beliefs updated.

### What is blocked and what is only warned

Blocked, nothing is written:

- unknown assertion kind, node type, predicate, or tag
- a tag value outside its closed set, or several values on a single-cardinality tag
- an edge whose endpoint types the predicate does not allow
- `edge_archive` against an edge that is not currently active
- a `source_identifier` that is not registered in `graph/sources.jsonl`
- an unknown `method`, or a hand-supplied `id` or `asserted_at`

Warned, the write still happens:

- no `source_identifier` and no `derived_from`, so the claim has no lineage root
- no `confidence_band`
- an edge endpoint that no node exists for yet
- a node whose type has a required-tag contract that is still unmet

**Treat every warning as a defect you introduced.** They are exactly what the
hygiene score counts. If you are asserting from a document, register the
document in `graph/sources.jsonl` first and cite it. If you are inferring,
say so: `--method agent --source src:agent-inference` is honest, and the low
reliability prior on that source is what makes the resulting belief honest too.

## 3. The read path

Read before you write. Duplicate nodes are the most common way an agent damages
a mind, and they are unrecoverable in the sense that only a new assertion can
paper over them.

```sh
scripts/search.sh "customer org"            # vault, derived graph, and the log
scripts/search.sh "broker" --vault          # notes only
scripts/search.sh "n:some-thing" --log      # every assertion touching an id

scripts/neighbors.sh n:some-thing           # one hop, with titles and belief
scripts/neighbors.sh n:some-thing --all     # include archived edges

jq -c 'select(.type=="decision")' graph/nodes.jsonl
jq -c 'select(.status=="contested")' graph/projections.jsonl
jq -c 'select(.src=="n:some-thing" or .dst=="n:some-thing")' graph/edges.jsonl
python3 scripts/dream.py --explain n:some-thing   # the evidence behind a belief

cat vault/some-thing.md                     # the human-readable face of a node
```

Which of these you reach for is itself being recorded (tool-reach). Prefer the
recipe that answers your question directly over reading whole files.

## 4. The scripts

| Script | What it does | When to run it |
|---|---|---|
| `scripts/assert.py` | validate, append, re-fold | every write, always |
| `scripts/project.py` | fold the log into `nodes.jsonl`, `edges.jsonl`, vault notes | after any manual recovery; `--check` reports drift |
| `scripts/dream.py` | the epistemic layer, `evidence_weighted/v0` | after a batch of writes, before you report beliefs |
| `scripts/check.py` | the invariant sweep | before you finish a session, always |
| `scripts/hygiene.py` | the measurement, appends to `graph/hygiene-history.jsonl` | at the end of a working session |
| `scripts/promote.py --dry-run` | the replay mapping into a SkaldOS tenant | when someone asks what promotion would carry |

They are Python standard library only, deliberately: a mind must run anywhere
`python3` exists, with no install step.

A normal session looks like this:

```sh
scripts/search.sh "the thing you are about to claim"   # do not duplicate
scripts/assert.py ... node ...                          # then the nodes
scripts/assert.py ... edge ...                          # then the edges
python3 scripts/dream.py                                # refresh beliefs
git diff graph/projections.jsonl                        # what the mind now thinks
python3 scripts/check.py                                # must be clean
python3 scripts/hygiene.py                              # record the score
git add -A && git commit -m "..."                       # the log is the history
```

## 5. How to say things well

- **Every claim carries a source.** `--source src:x` where `src:x` is a row in
  `graph/sources.jsonl`. If the source is new, add the row first, with an honest
  `reliability` prior. Priors are the dial the whole epistemic layer turns on.
- **Every claim carries a band.** `certain`, `likely`, `possible`,
  `speculative`. A band is your confidence in this claim from this source, not
  the source's own quality; that is what the reliability prior is for.
- **Repeating yourself is not corroboration.** The dreaming method takes the
  maximum weight per root source, so asserting the same edge five times from one
  document moves nothing. Two independent sources move a belief. That is the
  point.
- **Use `derived_from` when a claim comes from another claim.** Either an
  assertion id (`a-0042`) or a source id (`src:some-doc`). Lineage is what makes
  independence countable.
- **Retract with `archive`, and give a real reason.** The reason lands in the
  vault and in the projection's counter-evidence.
- **`grounded_by` is provenance, not argument.** Provenance-kind predicates are
  excluded from the epistemic walk on purpose. Do not use `supports` to mean
  "this document mentions this thing".

## 6. The vault

`vault/*.md` is an Obsidian projection, one note per node.

- The YAML frontmatter, the title heading, and the block between
  `<!-- mind:edges:start -->` and `<!-- mind:edges:end -->` are **machine-owned**.
  Editing them does nothing except create drift that `check.py` will report.
- The block between `<!-- mind:body:start -->` and `<!-- mind:body:end -->` is
  **human-owned**. Write anything there: notes, reasoning, quotes, todos. The
  fold copies it through byte for byte and will never touch it.
- To change a title, a type, or a tag, assert it. Do not edit the frontmatter.

## 7. Hosted compute

This mind ships two GitHub Actions workflows. They are the same scripts you run
locally, run on a machine that does not forget.

| Workflow | Trigger | What it does |
|---|---|---|
| `.github/workflows/project.yml` | every push | fold, dream, check, commit the derived files back |
| `.github/workflows/dream.yml` | nightly at 03:17 UTC, or manually | dream, measure hygiene, check, commit |

Both commit as `skaldos-mind[bot]` with `[skip ci]` in the message, which is
what stops a recompute from triggering another recompute.

What this means for you in practice: **push the log, let the robot push the
derived files.** You can commit derived files too and nothing breaks, since the
fold is deterministic and the workflow will simply find nothing to do. But if
you pushed assertions and then see a second commit arrive on the branch, that is
the workflow, not a conflict. Pull before your next write.

If a run fails, it is almost always `check.py` finding something real. Read the
job log, fix it by appending assertions, and push again.

## 8. What this mind is not

This is a sample, not the factory. Nothing here provides, or may be described as
providing:

- tenancy or row-level isolation
- classification or clearance-gated reads
- agent identity or a sandboxed write path
- concurrent multi-principal writes
- an audit trail that cannot be edited (git history is evidence, not enforcement)

If a second principal needs to write concurrently, or a classification boundary
appears in the data, or autonomy outgrows what this contract holds, that is a
promotion trigger. Say so; do not paper over it.

## 9. If something is already broken

- `python3 scripts/check.py` tells you what and where.
- Drift in a derived file: re-run `python3 scripts/project.py` and then
  `python3 scripts/dream.py`. The log wins, always.
- Someone edited the log by hand: do not fix it by editing it again. Say what
  you found, show `git log -p graph/assertions.jsonl`, and append corrective
  assertions instead. The corruption stays visible. That is correct.
