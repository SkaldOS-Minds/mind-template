# A SkaldOS Mind

This repository is a **mind**: one append-only assertion log, in the SkaldOS
Edda Standard (`edda/0`), plus everything derived from it. Any agent that can
run `python3`, `git`, `grep` and `jq` can operate it.

**Read [`AGENTS.md`](AGENTS.md) first.** It is the operating contract, and it is
written for whoever or whatever is about to make a claim here.

## Layout

```
AGENTS.md                    the operating contract, as prompt
ontology/ontology.json       node types, predicates, tags, bands, dream config
graph/
  assertions.jsonl           APPEND-ONLY. The single source of truth.
  sources.jsonl              source registry and reliability priors
  nodes.jsonl                derived
  edges.jsonl                derived
  projections.jsonl          derived: the epistemic layer
  hygiene-history.jsonl      the measurement, as a time series
  dream-history.jsonl        when beliefs were recomputed, and what moved
vault/                       Obsidian projection, one note per node
workflows/                   long-running workflow files; README.md is the spec
scripts/                     stdlib-only Python, plus two shell read recipes
.github/workflows/           push-time recompute and the nightly dream
```

`workflows/` and `.github/workflows/` are different things that share a word.
The first holds this mind's own standing instructions — a prose goal plus the
budget and tools a run of it is allowed. The second is GitHub Actions.

Everything outside `graph/assertions.jsonl`, `graph/sources.jsonl`,
`ontology/ontology.json` and the human-owned regions of `vault/*.md` is
disposable. Delete it, run `python3 scripts/project.py && python3
scripts/dream.py`, and you get the same bytes back.

## First five minutes

```sh
python3 scripts/check.py        # clean, and tells you this mind is not named yet
```

Fit `ontology/ontology.json` to your subject and register your real sources in
`graph/sources.jsonl` before there is data shaped by the wrong vocabulary. Then
make the first claim: that write names the mind, after the directory you cloned
into, so clone into `mind-<something-you-mean>` or export `MIND_SLUG` first.
`AGENTS.md` section 0 covers all of it.

## Writing

One command, always:

```sh
scripts/assert.py --band certain --source src:operator --method human --by you \
  node --id n:some-thing --type concept --title "Some thing"
```

Never edit `graph/assertions.jsonl` by hand. If a claim was wrong, append a
correction or an `edge_archive`. The mind keeps the record of having been wrong,
which is most of what makes it worth having.

## Hosted compute

Every push triggers a fold, a dream and an invariant sweep, and the derived
files are committed back by `skaldos-mind[bot]`. A nightly job re-dreams and
appends a hygiene measurement. Push the log; let the robot push the rest.

## Staying authenticated

You need no GitHub account to work here. The Mind Hub mints a token scoped to
this one repository, good for an hour, and `scripts/git-credential-mind-hub.py`
fetches a fresh one whenever `git` asks. Install it once per clone and stop
thinking about it. `AGENTS.md` section 8 has the two lines.

## What this is not

A sample, not a factory. No tenancy, no classification-gated reads, no agent
identity, no concurrent multi-principal writes, and a git history that is
evidence rather than enforcement. `AGENTS.md` section 9 says when that stops
being good enough.
