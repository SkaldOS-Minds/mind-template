#!/usr/bin/env python3
"""workflow.py: the `workflows/` file format, its parser and its validator.

A workflow file is frontmatter plus a prose goal. `workflows/README.md` in a
mind is the normative spec; this module is its reference implementation, and
`check.py` is what runs it over a repository.

Stdlib only, like the rest of the rig: a mind must run anywhere `python3`
exists with nothing installed. That rules out a YAML library, so the
frontmatter this parser accepts is a deliberately restricted subset of YAML —
flat `key: value` lines, one level of nesting under `budget`, inline lists,
simple scalars. Everything outside the subset is REFUSED by name
(`frontmatter-unsupported`) rather than half-parsed. A file that this parser
cannot represent exactly is a file the rig will not pretend to understand.

Every problem carries a stable error name. The names are the contract three
later items consume, so they change only with the spec.
"""
import re
from pathlib import Path

#: The directory a mind keeps its workflow files in.
WORKFLOWS_DIRNAME = "workflows"

#: Files in that directory that are documentation, not workflows.
NON_WORKFLOW_FILENAMES = frozenset({"README.md"})

REQUIRED_KEYS = ("name", "budget", "tools")
OPTIONAL_KEYS = ("schedule",)
KNOWN_KEYS = REQUIRED_KEYS + OPTIONAL_KEYS

#: The closed ceiling vocabulary. A budget must carry at least one of these,
#: and every one it carries must be a positive finite number.
BUDGET_CEILINGS = ("usd", "usd_per_step", "steps", "wall_clock_minutes")

#: The closed tool vocabulary: pi's built-in tools plus the SkaldOS package
#: tools, under the names those tools actually register. `powershell` is
#: deliberately absent — a W-v1 run executes in a Linux sandbox.
TOOL_VOCABULARY = (
    "bash",
    "edit",
    "find",
    "grep",
    "ls",
    "read",
    "write",
    "mind_search",
    "mind_node",
    "mind_neighbors",
    "mind_assert",
    "mind_dream",
    "mind_delegate",
    "read_document",
)

NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
KEY_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*):(.*)")
#: Numbers are ASCII digits only. Without re.ASCII, `\d` also matches Arabic-
#: Indic, Devanagari and every other Unicode decimal digit, so `usd: ٥` would
#: become the int 5 here while a loader written to the README read a string.
#: Outside the ASCII digits a scalar is a string, and a ceiling that is a
#: string is caught as `budget-unbounded`.
INT_RE = re.compile(r"-?\d+", re.ASCII)
FLOAT_RE = re.compile(r"-?(?:\d+\.\d*|\.\d+)", re.ASCII)

#: 24-hour clock: 00:00 through 23:59. `[0-2]\d` would also admit 24:00-29:59,
#: times that never occur, and a schedule holding one never fires.
_HHMM = r"(?:[01]\d|2[0-3]):[0-5]\d"
SCHEDULE_RE = re.compile(
    rf"hourly|daily {_HHMM}|weekly (?:mon|tue|wed|thu|fri|sat|sun) {_HHMM}",
    re.ASCII,
)

#: Scalars YAML 1.1 would silently turn into booleans or nulls. The subset
#: refuses them instead of guessing which era of YAML the author meant.
YAML_TRAPS = frozenset(
    {"null", "~", "yes", "no", "on", "off", "none", "nil", "inf", "-inf", ".inf", "nan", ".nan"}
)

#: Value syntax the subset does not represent, by leading character.
UNSUPPORTED_LEAD = {
    "&": "an anchor",
    "*": "an alias",
    "!": "a tag",
    "|": "a literal block scalar",
    ">": "a folded block scalar",
    "{": "a flow mapping",
    "%": "a directive",
    "`": "a reserved indicator",
    "@": "a reserved indicator",
}


class WorkflowProblem(Exception):
    """One named refusal. `name` is stable; `detail` is for a human."""

    def __init__(self, name, detail, line=None):
        self.name = name
        self.detail = detail
        self.line = line
        super().__init__(str(self))

    def __str__(self):
        where = f" (line {self.line})" if self.line else ""
        return f"{self.name}: {self.detail}{where}"


def _unsupported(detail, line=None):
    return WorkflowProblem("frontmatter-unsupported", detail, line)


def _syntax(detail, line=None):
    return WorkflowProblem("frontmatter-syntax", detail, line)


# --------------------------------------------------------------------------
# the restricted parser
# --------------------------------------------------------------------------


def split_frontmatter(text):
    """(frontmatter lines, first frontmatter line number, body). Raises."""
    if text.startswith("﻿"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        raise WorkflowProblem(
            "frontmatter-missing",
            "the file must open with a line that is exactly '---'",
            1,
        )
    for i in range(1, len(lines)):
        if lines[i] == "---":
            return lines[1:i], 2, "\n".join(lines[i + 1:])
    raise WorkflowProblem(
        "frontmatter-unterminated",
        "the frontmatter block is never closed by a line that is exactly '---'",
        1,
    )


def _outside_quotes(text):
    """Every character of `text` that sits outside a quoted scalar.

    The quote characters themselves are not yielded, so a caller asking
    "does this hold a bracket?" gets the answer for the structure and not for
    the contents of a string the author quoted on purpose.
    """
    quote = None
    for ch in text:
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            continue
        yield ch


def _split_flow_items(inner):
    """Split an inline list's contents on commas, respecting quotes.

    Takes no line number because it raises nothing. The single caller reaches
    here having already proven the quotes balanced — its forward scan stops at
    the first `]` outside quotes and raises the unterminated-quote refusal
    itself when it finds none — so this splitter has no unbalanced input to
    refuse, and no longer carries a guard that could not fire.
    """
    items, buf, quote = [], "", None
    for ch in inner:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf += ch
        elif ch == ",":
            items.append(buf)
            buf = ""
        else:
            buf += ch
    items.append(buf)
    return items


def _after_value(after, what, line):
    """Check what follows a value that has already closed itself.

    Nothing, or a comment — and YAML only reads '#' as a comment when a space
    precedes it. Pressed against the value ('"a"#c', '[a]#c') it is neither a
    comment nor anything else the subset represents, so it is refused instead
    of being dropped as if it had been one.
    """
    rest = after.strip()
    if not rest:
        return
    if rest.startswith("#") and after[:1] == " ":
        return
    raise _syntax(f"trailing text after {what}: {rest!r}", line)


def _flow_comment(item):
    """True if a raw inline-list item holds a '#' that YAML reads as a comment.

    That is one at the item's start or with a space before it, outside quotes.
    A comment inside an inline list runs to end of line, so it swallows the
    list's own ']' and the list never closes: real YAML refuses these, and so
    does this. A '#' pressed against the previous character ('a#b') is part of
    the plain scalar, which YAML allows and so does this.
    """
    previous, quote = " ", None
    for ch in item:
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and previous == " ":
            return True
        previous = ch
    return False


def _parse_quoted(token, line):
    quote = token[0]
    if "\\" in token:
        raise _unsupported("an escape sequence in a quoted scalar", line)
    end = token.find(quote, 1)
    if end == -1:
        raise _syntax("an unterminated quoted scalar", line)
    _after_value(token[end + 1:], "a quoted scalar", line)
    return token[1:end]


def _parse_bare(token, line):
    token = token.split(" #", 1)[0].strip()
    if not token:
        raise _syntax("a key with no value", line)
    if token.lower() in YAML_TRAPS:
        raise _unsupported(
            f"the scalar {token!r}, whose meaning differs between YAML versions; "
            "write true/false, a number, or a quoted string",
            line,
        )
    if token in ("true", "false"):
        return token == "true"
    if INT_RE.fullmatch(token):
        digits = token[1:] if token[0] == "-" else token
        if len(digits) > 1 and digits[0] == "0":
            raise _unsupported(
                f"the scalar {token!r}, which YAML 1.1 reads as octal and YAML 1.2 "
                "reads as decimal; write it without the leading zero, or quote it "
                "to mean the string",
                line,
            )
        return int(token)
    if FLOAT_RE.fullmatch(token):
        return float(token)
    if token in ("True", "False", "TRUE", "FALSE"):
        raise _unsupported(f"the scalar {token!r}; booleans are spelled true and false", line)
    return token


def _parse_value(raw, line, allow_list=True):
    token = raw.strip()
    if not token:
        raise _syntax("a key with no value", line)
    lead = token[0]
    if lead in UNSUPPORTED_LEAD:
        raise _unsupported(f"{UNSUPPORTED_LEAD[lead]} ({lead!r})", line)
    if lead == "[":
        if not allow_list:
            raise _unsupported("a list nested inside a mapping", line)
        # The closing ']' is the first one outside quotes, not the last one in
        # the line: `[a, b] # ]` closes at the first, and rfind would take the
        # one inside the comment and mis-read the list.
        close, quote = -1, None
        for index, ch in enumerate(token[1:], 1):
            if quote is not None:
                if ch == quote:
                    quote = None
            elif ch in "\"'":
                quote = ch
            elif ch == "]":
                close = index
                break
        if quote is not None:
            raise _syntax("an inline list has an unterminated quoted item", line)
        if close == -1:
            raise _syntax("an inline list that is never closed with ']'", line)
        inner = token[1:close]
        # Before the trailing-text check: a nested list closes at its own ']',
        # so the outer list's tail is what follows, and the honest name for
        # `[[a, b], c]` is the nesting, not the ', c]' left over.
        # Only a bracket that is part of the list's own structure nests a
        # collection. One inside a quoted item is a character in a string —
        # `["a[b"]` is valid YAML and means the one-item list `a[b` — so the
        # scan skips quoted content rather than refusing a file the spec allows.
        if any(ch in "[{" for ch in _outside_quotes(inner)):
            raise _unsupported("a nested collection inside an inline list", line)
        _after_value(token[close + 1:], "an inline list", line)
        if not inner.strip():
            return []
        out = []
        for item in _split_flow_items(inner):
            if _flow_comment(item):
                raise _syntax(
                    "a comment inside an inline list, which would run past the "
                    "list's own ']'",
                    line,
                )
            item = item.strip()
            if not item:
                raise _syntax("an empty item in an inline list", line)
            if item[0] in "\"'":
                out.append(_parse_quoted(item, line))
            else:
                out.append(_parse_bare(item, line))
        return out
    if lead in "\"'":
        return _parse_quoted(token, line)
    return _parse_bare(token, line)


def parse_frontmatter(lines, first_line=1):
    """The restricted subset, as a dict. Raises WorkflowProblem."""
    data = {}
    open_map = None  # the key whose nested block we are inside, or None
    for offset, raw in enumerate(lines):
        line = first_line + offset
        if "\t" in raw:
            raise _unsupported("a tab character; indent with spaces", line)
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith("-"):
            raise _unsupported("a block sequence ('- item'); use an inline list", line)
        indent = len(raw) - len(raw.lstrip(" "))
        if indent not in (0, 2):
            raise _unsupported(
                f"an indent of {indent} spaces; the subset allows top-level keys "
                "and one nested level indented by exactly 2 spaces",
                line,
            )
        match = KEY_RE.fullmatch(stripped)
        if not match:
            raise _syntax(f"a line that is not 'key: value': {stripped!r}", line)
        key, rest = match.group(1), match.group(2)
        if rest and not rest.startswith(" "):
            raise _syntax("a value must be separated from its key by a space", line)
        if indent == 0:
            if key in data:
                raise WorkflowProblem(
                    "frontmatter-duplicate-key", f"the key '{key}' appears twice", line
                )
            if rest.strip() == "" or rest.strip().startswith("#"):
                data[key] = {}
                open_map = key
            else:
                data[key] = _parse_value(rest, line)
                open_map = None
        else:
            if open_map is None:
                raise _unsupported(
                    "an indented line that opens no nested mapping", line
                )
            if rest.strip() == "" or rest.strip().startswith("#"):
                raise _unsupported(
                    f"the nested key '{key}' with no value, which opens a second "
                    f"level of nesting under '{open_map}'; the subset allows one, "
                    "so a key at this indent must carry its value on the same line",
                    line,
                )
            if key in data[open_map]:
                raise WorkflowProblem(
                    "frontmatter-duplicate-key",
                    f"the key '{key}' appears twice under '{open_map}'",
                    line,
                )
            data[open_map][key] = _parse_value(rest, line, allow_list=False)
    return data


def parse(text):
    """(frontmatter dict, body string). Raises WorkflowProblem."""
    lines, first, body = split_frontmatter(text)
    return parse_frontmatter(lines, first), body


# --------------------------------------------------------------------------
# the validator
# --------------------------------------------------------------------------


def _check_budget(budget, problems):
    if not isinstance(budget, dict):
        problems.append(
            WorkflowProblem(
                "budget-invalid",
                "budget must be a mapping of ceilings, not a single scalar",
            )
        )
        return
    if not budget:
        problems.append(
            WorkflowProblem(
                "budget-empty",
                "budget carries no ceiling; name at least one of "
                + ", ".join(BUDGET_CEILINGS),
            )
        )
        return
    for key in sorted(budget):
        if key not in BUDGET_CEILINGS:
            problems.append(
                WorkflowProblem(
                    "budget-unknown-ceiling",
                    f"'{key}' is not a ceiling; the vocabulary is "
                    + ", ".join(BUDGET_CEILINGS),
                )
            )
            continue
        value = budget[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(
                WorkflowProblem(
                    "budget-unbounded",
                    f"ceiling '{key}' is {value!r}, which bounds nothing; "
                    "a ceiling is a positive number",
                )
            )
        elif value <= 0:
            problems.append(
                WorkflowProblem(
                    "budget-unbounded",
                    f"ceiling '{key}' is {value!r}; a run under a zero or negative "
                    "ceiling either never starts or never stops",
                )
            )


def _check_tools(tools, problems):
    if not isinstance(tools, list):
        problems.append(
            WorkflowProblem("tools-invalid", "tools must be an inline list, e.g. [mind_search]")
        )
        return
    if not tools:
        problems.append(
            WorkflowProblem("tools-empty", "tools is empty; a run with no tools can do nothing")
        )
        return
    seen = set()
    for tool in tools:
        if not isinstance(tool, str):
            problems.append(WorkflowProblem("tools-invalid", f"{tool!r} is not a tool name"))
            continue
        if tool in seen:
            problems.append(WorkflowProblem("tools-duplicate", f"'{tool}' is listed twice"))
        seen.add(tool)
        if tool not in TOOL_VOCABULARY:
            problems.append(
                WorkflowProblem(
                    "tools-unknown",
                    f"'{tool}' is not in the tool vocabulary; the vocabulary is "
                    + ", ".join(TOOL_VOCABULARY),
                )
            )


def validate_text(text, stem=None):
    """Every problem in one workflow file's text, as WorkflowProblem objects.

    A parse failure is terminal: the list is that one problem and nothing else,
    because nothing after it can be trusted.
    """
    if not text.strip():
        return [WorkflowProblem("file-empty", "the file is empty")]
    try:
        data, body = parse(text)
    except WorkflowProblem as problem:
        return [problem]

    problems = []
    if not body.strip():
        problems.append(
            WorkflowProblem(
                "body-empty",
                "the prose goal is empty; the body after the frontmatter is what "
                "the run is asked to do",
            )
        )
    for key in sorted(data):
        if key not in KNOWN_KEYS:
            problems.append(
                WorkflowProblem(
                    "unknown-key",
                    f"'{key}' is not part of the workflow contract; the keys are "
                    + ", ".join(KNOWN_KEYS),
                )
            )
    if "budget" not in data:
        problems.append(
            WorkflowProblem(
                "budget-missing",
                "no budget; a workflow file without a ceiling is refused "
                "(long-running workflows plan section 2)",
            )
        )
    else:
        _check_budget(data["budget"], problems)
    for key in REQUIRED_KEYS:
        if key != "budget" and key not in data:
            problems.append(WorkflowProblem("missing-key", f"no '{key}'"))

    if "name" in data:
        name = data["name"]
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            problems.append(
                WorkflowProblem(
                    "name-invalid",
                    f"{name!r} is not a slug (lowercase letters, digits, single hyphens)",
                )
            )
        elif stem is not None and name != stem:
            problems.append(
                WorkflowProblem(
                    "name-mismatch",
                    f"name is '{name}' but the file is '{stem}.md'; they must agree "
                    "so a run names the file it came from",
                )
            )
    if "tools" in data:
        _check_tools(data["tools"], problems)
    if "schedule" in data:
        schedule = data["schedule"]
        if not isinstance(schedule, str) or not SCHEDULE_RE.fullmatch(schedule):
            problems.append(
                WorkflowProblem(
                    "schedule-invalid",
                    f"{schedule!r} is not a schedule; write 'hourly', 'daily HH:MM', "
                    "or 'weekly <mon..sun> HH:MM'",
                )
            )
    return problems


def validate_file(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [WorkflowProblem("file-unreadable", "the file is not valid UTF-8")]
    except OSError as exc:
        # A file the sweep listed but cannot open — permissions, a vanished
        # file, a broken link, a directory named `*.md`. The sweep reports it
        # by name like every other fault instead of ending in a traceback; the
        # exit code is non-zero either way, so nothing passes that should not.
        return [
            WorkflowProblem(
                "file-unreadable",
                f"the file cannot be read: {exc.strerror or type(exc).__name__}",
            )
        ]
    return validate_text(text, stem=path.stem)


def workflow_files(directory: Path):
    """Every workflow file in a `workflows/` directory, in a stable order.

    `README.md` is the spec that lives beside them, not a workflow, and is the
    one filename excluded.
    """
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.glob("*.md")
        if p.is_file() and p.name not in NON_WORKFLOW_FILENAMES
    )
