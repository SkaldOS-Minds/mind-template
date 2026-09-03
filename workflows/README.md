# The `workflows/` format

A workflow file is a standing instruction to a mind: a prose goal, plus the
ceilings and the tools a run of that goal is allowed. It is the unit a long
run is launched from, and it is the only place a run's budget is written.

This file is the **normative spec**. `scripts/workflow.py` is its reference
implementation and `scripts/check.py` runs it over every file here on every
sweep. Anything a loader elsewhere in the vertical accepts that this document
does not describe is a bug in the loader, not an extension of the format.

Not to be confused with `.github/workflows/`, which is GitHub Actions and has
nothing to do with this format beyond the word.

## Which files are workflows

Every `workflows/*.md` in a mind, **except `README.md`**, which is this spec
and is never validated as a workflow. Subdirectories are not scanned. A mind
with no `workflows/` directory is complete; nothing requires one.

## Shape

```
---
<frontmatter>
---

<prose goal>
```

The first line of the file must be exactly `---`. The frontmatter block ends
at the first later line that is exactly `---`. Everything after that line is
the body. A UTF-8 BOM is stripped and CRLF line endings are accepted; nothing
else about the framing is flexible.

## The four keys

`name`, `budget` and `tools` are **required**. `schedule` is optional. No other
key is permitted; an unrecognised key is refused rather than ignored, so that a
typo (`tool:`, `budgets:`) fails loudly instead of dropping a ceiling.

### `name` — required, slug

Lowercase letters and digits, separated by single hyphens: `hygiene-review`.
It must equal the file's own stem, so `hygiene-review.md` carries
`name: hygiene-review`. A run reports the name; the name has to lead back to
the file without a lookup table.

### `budget` — required, and it must bound something

A mapping of ceilings, at least one of them, each a positive number:

| Ceiling | Unit | Bounds |
|---|---|---|
| `usd` | dollars | total model spend across the whole run |
| `usd_per_step` | dollars | spend on any single step |
| `steps` | count | steps the run may take |
| `wall_clock_minutes` | minutes | elapsed time from launch |

The vocabulary is closed. A ceiling name outside it is refused, because a
ceiling nobody enforces is worse than no ceiling at all.

**A workflow file with no `budget`, an empty `budget`, or any ceiling that is
zero, negative, or not a number is REFUSED.** This is a ratified ruling (the
long-running workflows plan, section 2), not a default that can be relaxed per
file. There is no way to spell "unlimited" in this format, and adding one would
be a change to the plan before it was a change to this spec.

Ceilings pause a run and ask; they do not kill it. Enforcement lives in the
package's `DelegationBroker`, which checks every ceiling **before** a step runs.
A breach moves the run to `input_required` with the ceiling named.

### `tools` — required allowlist

An inline list of tool names, at least one, each from this closed vocabulary:

`bash`, `edit`, `find`, `grep`, `ls`, `read`, `write`,
`mind_search`, `mind_node`, `mind_neighbors`, `mind_assert`, `mind_dream`,
`mind_delegate`, `read_document`

These are the names the tools actually register under, in pi and in the
`skaldos` package. A name outside the list is refused, so a workflow cannot
quietly ask for a tool that will never be granted. Duplicates are refused too.
`powershell` is deliberately absent: a W-v1 run executes in a Linux sandbox.

The list is an allowlist and nothing more. It cannot widen what the mind
already permits, and the privacy guard runs regardless of what is listed here.

### `schedule` — optional

One of exactly three forms:

```
schedule: hourly
schedule: daily 03:00
schedule: weekly tue 09:00
```

Weekday is `mon` `tue` `wed` `thu` `fri` `sat` `sun`. Times are 24-hour and
zero-padded, `00:00` through `23:59`, in the local timezone of the machine that
fires them. `24:00` is not midnight here and `25:00` is not a typo the format
forgives: a time outside that range is refused, because a scheduler holding a
time that never occurs is a workflow that silently never runs.

**Read this before you write a schedule.** In W-v1 a schedule fires **only
while the SkaldOS desktop app is running.** There is no server-side clock
behind it. If the app is closed at 03:00, the 03:00 run does not happen, and it
does not happen later either — a missed occurrence is skipped, not queued.
This is not cron and must not be described as cron. Unattended scheduling needs
durable timers in the hub, which is W-v2's first item; until that ships, a
workflow whose value depends on running while nobody is at the machine should
carry no `schedule` and be launched from a conversation.

### The body

Everything after the closing `---`. This is the prose goal: what the run is
asked to do, in the language a person would use to ask a colleague. A file
whose body is empty or only whitespace is refused. There is no length limit
and no required structure.

## The frontmatter subset, and why it is restricted

The rig is stdlib-only Python, because a mind must run anywhere `python3`
exists with nothing installed. That rules out a YAML library, so the
frontmatter is a **deliberately restricted subset of YAML** that a small
hand-written parser handles exactly. The restriction is the feature: a file
using YAML the parser does not implement is **refused by name**, never
half-parsed into something that looks valid and means something else.

**Supported:**

- `key: value` at the left margin. Keys match `[A-Za-z_][A-Za-z0-9_]*`.
- One nested level, indented by exactly two spaces, under a key written with no
  value on its own line. `budget` is the only key that uses it.
- Scalars: integers (`40`), decimals (`1.50`), `true` / `false`, and strings.
  A string may be bare (`daily 03:00`) or quoted with `'` or `"`. A number is
  written with the ASCII digits `0`-`9` and nothing else: a scalar carrying any
  other digit character is a string, so `usd: ٥` is not the ceiling 5, it is a
  ceiling that bounds nothing and is refused as `budget-unbounded`.
- Inline lists: `[a, b, c]`, with scalar items only. `[]` is an empty list. A
  bracket inside a quoted item is a character in that string and not structure,
  so `["a[b"]` is the one-item list `a[b`, exactly as in YAML.
- Blank lines, and comment lines whose first non-space character is `#`.
- A trailing `# comment` after a value, with a space before the `#`. In an
  unquoted scalar this means everything from the first ` #` onward is a
  comment, so a value that needs a literal `#` after a space must be quoted.
  A `#` with no space before it is part of the value, exactly as in YAML:
  `name: a#b` is the string `a#b`.

**Refused, each by name:**

- Block sequences (`- item`). Use an inline list.
- Block scalars (`|`, `>`), anchors (`&`), aliases (`*`), tags (`!`),
  directives (`%`), flow mappings (`{...}`).
- Nesting deeper than one level, or any indent other than 0 or 2 spaces.
- Tab characters anywhere in the frontmatter.
- Escape sequences inside quoted scalars.
- A comment inside an inline list (`tools: [read # note, write]`). A YAML
  comment runs to the end of the line, so it would swallow the list's own `]`
  and the list would never close; real YAML refuses these and so does this.
- A `#` pressed against a value that has already closed itself — `name: "x"#c`,
  `tools: [read]#c`. YAML does not read those as comments, so neither are they
  dropped here; they are `frontmatter-syntax`, trailing text.
- A key repeated in the same mapping.
- `null`, `~`, `yes`, `no`, `on`, `off`, `nan`, `inf` and their kin, whose
  meaning differs between YAML 1.1 and 1.2. Write `true` / `false`, a number,
  or a quoted string and there is nothing to guess.
- An integer written with a leading zero (`steps: 010`), for the same reason:
  YAML 1.1 reads it as octal 8 and YAML 1.2 as decimal 10. Drop the zero, or
  quote it if the string is what was meant. A single `0` is unambiguous and is
  a number.

There is no format-version key. The rig versions the format, not the file: a
file a newer rig cannot read fails loudly with a named error, which is the
outcome a version field would have bought and one fewer thing to keep in sync.

## Error names

Every refusal carries a stable name. These names are the contract; the prose
after them is for a human and may be reworded.

| Name | Means |
|---|---|
| `file-empty` | the file has no content |
| `file-unreadable` | the file is not valid UTF-8, or cannot be opened at all |
| `frontmatter-missing` | the file does not open with `---` |
| `frontmatter-unterminated` | the frontmatter block is never closed |
| `frontmatter-syntax` | a line inside the block is not `key: value` |
| `frontmatter-unsupported` | valid YAML that this subset does not represent |
| `frontmatter-duplicate-key` | a key appears twice in one mapping |
| `unknown-key` | a frontmatter key outside the four |
| `missing-key` | `name` or `tools` absent |
| `name-invalid` | `name` is not a slug |
| `name-mismatch` | `name` and the filename disagree |
| `budget-missing` | no `budget` at all |
| `budget-invalid` | `budget` is a scalar, not a mapping of ceilings |
| `budget-empty` | `budget` names no ceiling |
| `budget-unknown-ceiling` | a ceiling outside the vocabulary |
| `budget-unbounded` | a ceiling that is zero, negative, or not a number |
| `tools-invalid` | `tools` is not a list of names |
| `tools-empty` | `tools` is an empty list |
| `tools-unknown` | a tool outside the vocabulary |
| `tools-duplicate` | a tool listed twice |
| `schedule-invalid` | `schedule` is outside the three forms |
| `body-empty` | no prose goal |

A parse failure is terminal: the file reports that one problem and no others,
because nothing after a line the parser could not read can be trusted. Every
problem after a successful parse is reported together, so one sweep names
every fault in the file.

All of these are **errors**. `check.py` exits non-zero on any of them; none of
them is a warning.

## Changing this spec

The tool vocabulary, the ceiling vocabulary and the four keys live in three
places that must move together: this file, `scripts/workflow.py`, and the
package loader in `skaldos-pi`. The vertical rule applies — a capability that
needs a fifth key is a design change in `skaldos-mind`, not a local extension
in a client.

## Example

`hygiene-review.md` in this directory is a valid workflow, kept valid by
`check.py` on every sweep of every mind stamped from the template. It ships
without a `schedule` on purpose: a scheduled example would start firing in
every new mind the first time its owner opened the app.
