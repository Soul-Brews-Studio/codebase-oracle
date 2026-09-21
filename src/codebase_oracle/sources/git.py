"""git history to events: commits, file changes, submodule bumps.

One `git log` walk produces all three. The diff section is read with BOTH `--raw` and
`--numstat`, which is safe to combine because their lines are unambiguous: raw lines
start with ':' and numstat lines start with a digit or '-'. Raw carries the file modes,
which is the only way to see that a change is a gitlink rather than a file, and it
carries the old and new object ids, which for a gitlink ARE the submodule's old and new
commit.
"""

from __future__ import annotations

import re
from typing import Iterator

from ..gitio import git, git_ok
from ..ids import commit_id, file_change_id, submodule_bump_id
from ..models import EventRow
from ..time import to_utc_iso

# \x1e starts a commit record, \x1f separates its fields. Neither can appear in a git
# object id, a date, or a name, and a commit message containing them would be
# pathological. A newline separator cannot be used: %B is multi-line by nature.
_REC = "\x1e"
_FLD = "\x1f"
_FORMAT = _REC + _FLD.join(["%H", "%aI", "%cI", "%an", "%ae", "%P", "%B"])

_GITLINK_MODE = "160000"

_RAW = re.compile(
    r"^:(?P<old_mode>\d{6}) (?P<new_mode>\d{6}) (?P<old_sha>[0-9a-f]+) "
    r"(?P<new_sha>[0-9a-f]+) (?P<status>[A-Z])\d*\t(?P<path>.*)$"
)

_REFS = re.compile(r"#(\d+)")

# The fleet writes this trailer both ways — "Co-Authored-By" and "Co-Author-By" — so
# both are accepted. The optional 'ed' is not a typo.
_COAUTHOR = re.compile(
    r"^Co-Author(?:ed)?-By:\s*(?P<name>[^<\n]+?)\s*(?:<(?P<email>[^>]*)>)?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# An agent is only recognised from this list. Anything else in a Co-Authored-By trailer
# is a HUMAN collaborator and must not be recorded as the agent that wrote the commit —
# guessing from the trailer alone would mislabel every human co-author as a model.
_AGENTS = {
    "claude": "Claude",
    "gpt": "GPT",
    "codex": "Codex",
    "gemini": "Gemini",
    "copilot": "Copilot",
    "devin": "Devin",
    "aider": "Aider",
    "cursor": "Cursor",
}


def parse_authors(author_name: str, body: str) -> tuple[str, str, str]:
    """(human, agent, model).

    git reports the committer's configured identity for every commit, including the
    ones an agent wrote — on this fleet that means Nat's name on work Sonnet produced.
    The model appears only in the trailer, so without reading it "who wrote this
    codebase" has no answer at all.
    """
    for m in _COAUTHOR.finditer(body or ""):
        name = (m.group("name") or "").strip()
        head, _, rest = name.partition(" ")
        vendor = _AGENTS.get(head.lower())
        if vendor:
            return author_name, vendor, rest.strip()
    return author_name, "", ""


def _rev_range(cwd: str, last_sha: str) -> list[str]:
    """`last_sha..HEAD` when the watermark is still reachable, else the whole history.

    A rebase or a force-push strands the recorded sha, and `--is-ancestor` reports both
    "unknown object" and "not an ancestor" through a non-zero exit. Falling back to a
    full walk is safe precisely because ids are content-addressed: re-reading known
    commits rewrites identical rows rather than duplicating them.
    """
    if not last_sha:
        return ["HEAD"]
    if git_ok(cwd, "merge-base", "--is-ancestor", last_sha, "HEAD"):
        return [f"{last_sha}..HEAD"]
    return ["HEAD"]


def head_sha(cwd: str) -> str:
    return git(cwd, "rev-parse", "HEAD", check=False)


def read(unit: str, cwd: str, last_sha: str = "", since: str = "") -> Iterator[EventRow]:
    """Every event this unit's history produces, oldest first."""
    args = [
        "log",
        "--reverse",
        f"--format={_FORMAT}",
        "--raw",
        "--numstat",
        "--no-renames",
        "--abbrev=40",
    ]
    if since:
        args.append(f"--since={since}")
    args.extend(_rev_range(cwd, last_sha))

    out = git(cwd, *args, check=False)
    if not out:
        return

    for record in out.split(_REC):
        if not record.strip():
            continue
        yield from _one_commit(unit, record)


def _one_commit(unit: str, record: str) -> Iterator[EventRow]:
    fields = record.split(_FLD)
    if len(fields) < 7:
        return
    sha, ts_a, ts_c, an, _ae, parents, tail = fields[:7]
    sha = sha.strip()
    if not sha:
        return

    # %B runs to the end of the record, and the diff section follows it. Split them on
    # the first line that looks like diff output rather than guessing at blank lines,
    # because commit messages contain blank lines freely.
    body_lines: list[str] = []
    diff_lines: list[str] = []
    in_diff = False
    for line in tail.split("\n"):
        if not in_diff and (line.startswith(":") or _is_numstat(line)):
            in_diff = True
        (diff_lines if in_diff else body_lines).append(line)

    body = "\n".join(body_lines).strip()
    human, agent, model = parse_authors(an, body)
    ts_author = to_utc_iso(ts_a)
    ts_commit = to_utc_iso(ts_c)
    refs = " ".join(f"#{n}" for n in dict.fromkeys(_REFS.findall(body)))

    def row(**kw) -> EventRow:
        base = dict(
            unit=unit,
            sha=sha,
            ts_author=ts_author,
            ts_commit=ts_commit,
            author_human=human,
            author_agent=agent,
            author_model=model,
            refs=refs,
            path="",
            text="",
            number=0,
            insertions=0,
            deletions=0,
            from_sha="",
            to_sha="",
        )
        base.update(kw)
        return EventRow(**base)

    yield row(uid=commit_id(unit, sha), kind="commit", text=body, from_sha=parents.strip())

    # numstat carries the line counts, raw carries the modes. Join them by path.
    stats: dict[str, tuple[int, int]] = {}
    for line in diff_lines:
        if _is_numstat(line):
            add, dele, path = line.split("\t", 2)
            stats[path] = (_num(add), _num(dele))

    for line in diff_lines:
        m = _RAW.match(line)
        if not m:
            continue
        path = m["path"]
        if _GITLINK_MODE in (m["old_mode"], m["new_mode"]):
            yield row(
                uid=submodule_bump_id(unit, sha, path),
                kind="submodule-bump",
                path=path,
                text=f"{path} {m['old_sha'][:12]}..{m['new_sha'][:12]}",
                from_sha=m["old_sha"],
                to_sha=m["new_sha"],
            )
            continue
        add, dele = stats.get(path, (0, 0))
        yield row(
            uid=file_change_id(unit, sha, path),
            kind="file-change",
            path=path,
            text=path,
            insertions=add,
            deletions=dele,
        )


def _is_numstat(line: str) -> bool:
    if "\t" not in line:
        return False
    head = line.split("\t", 1)[0]
    return head == "-" or head.isdigit()


def _num(v: str) -> int:
    """numstat writes '-' for binary files."""
    return int(v) if v.isdigit() else 0
