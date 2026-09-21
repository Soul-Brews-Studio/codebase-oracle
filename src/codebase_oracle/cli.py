"""cbo — five commands over one codebase's event log.

The `common` parent parser with `default=argparse.SUPPRESS` is load-bearing, not
decoration. Without a parent, `cbo status --json` exits 2 because the subparser has
never heard of `--json`. With an ordinary default on the parent, the subparser's default
overwrites whatever the top-level flag set. SUPPRESS means an absent flag sets no
attribute at all.

That last property is also why `Opts` exists. Reading a SUPPRESSed flag inline means
`getattr(a, "json", False)` at every use site, against an object mypy only knows as
`Namespace` — so a mistyped flag name becomes a silent `False` rather than an error.
The Namespace is converted ONCE, in `opts()`, and nothing downstream touches argparse.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .gitio import GitError, git
from .indexer import ensure_excluded, run_index, store_dir
from .store import Store, sql_str
from .time import local_date_time, zone_offset
from .units import RepoNotFound, codebase_root, resolve_repo


@dataclass(frozen=True)
class Opts:
    """Every flag this CLI accepts, typed. Built once from the argparse Namespace."""

    json: bool = False
    path: str = ""
    repo: str = ""
    # index
    no_gh: bool = False
    full: bool = False
    # ranges
    since: str = ""
    until: str = ""
    # search / timeline
    query: str = ""
    unit: str = ""
    limit: int = 20
    kinds: tuple[str, ...] = field(default_factory=tuple)
    # show
    uid: str = ""


def opts(a: argparse.Namespace) -> Opts:
    """The single point of contact with argparse's untyped Namespace."""
    g: Callable[..., Any] = a.__dict__.get
    kind = g("kind") or []
    return Opts(
        json=bool(g("json", False)),
        path=str(g("path") or ""),
        repo=str(g("repo") or ""),
        no_gh=bool(g("no_gh", False)),
        full=bool(g("full", False)),
        since=str(g("since") or ""),
        until=str(g("until") or ""),
        query=str(g("query") or ""),
        unit=str(g("unit") or ""),
        limit=int(g("limit") or 0),
        kinds=tuple(str(k) for k in kind),
        uid=str(g("uid") or ""),
    )


def _root(o: Opts) -> str:
    """Which codebase this invocation is about.

    `--repo <name>` exists because the common question is asked from somewhere else:
    sitting in one repo and wanting to know what happened in another. It resolves a name
    through the ghq tree and then opens THAT codebase's own store — there is still no
    global index, only a different one.
    """
    if o.repo:
        return codebase_root(resolve_repo(o.repo))
    return codebase_root(o.path or os.getcwd())


def _store(o: Opts) -> tuple[str, Store]:
    root = _root(o)
    return root, Store(store_dir(root))


def _emit(o: Opts, payload: object, lines: list[str]) -> int:
    if o.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for line in lines:
            print(line)
    return 0


def _first_line(text: str | None, width: int) -> str:
    return (text or "").strip().split("\n")[0][:width]


# ------------------------------------------------------------------- commands


def cmd_index(o: Opts) -> int:
    root = _root(o)
    summary = run_index(root, with_gh=not o.no_gh, since=o.since, full=o.full)
    if o.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0
    for u in summary["units"]:
        mark = "" if u["indexed"] else "  (not initialised — bumps only)"
        print(f"  {u['unit']:<28} +{u['written']}{mark}")
    for err in summary["gh_errors"]:
        print(f"  gh skipped for {err['repo']}: {err['error']}", file=sys.stderr)
    return 0


def cmd_status(o: Opts) -> int:
    root, store = _store(o)
    if not store.has_index():
        # Deliberately distinct from "indexed, zero events" — see Store._existing.
        print(f"no index at {store_dir(root)} — run: cbo index", file=sys.stderr)
        return 1

    kinds = store.counts_by_kind()
    rows: list[dict[str, Any]] = []
    lines = [f"codebase  {root}", f"store     {store_dir(root)}", ""]

    for u in store.units():
        note = ""
        if not u["indexed"]:
            note = "  not initialised"
        else:
            head = git(str(u["path"]), "rev-parse", "HEAD", check=False)
            wm = store.watermark(str(u["unit"]), "git") or {}
            if not head:
                note = "  unreadable"
            elif wm.get("last_sha") and head != wm["last_sha"]:
                note = f"  STALE (HEAD {head[:8]} != indexed {str(wm['last_sha'])[:8]})"
        rows.append({**u, "stale": bool(note.strip())})
        lines.append(f"  {u['unit']:<28} {u['event_count']:>7} events  {u['url']}{note}")

    lines.append("")
    for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {k:<20} {n:>7}")
    lines.append("")
    lines.append(f"  times shown in local time ({zone_offset()}); stored UTC")

    return _emit(o, {"root": root, "units": rows, "kinds": kinds}, lines)


def cmd_search(o: Opts) -> int:
    _, store = _store(o)
    where = f"unit = '{sql_str(o.unit)}'" if o.unit else ""
    hits = store.search(o.query, limit=o.limit, where=where)
    lines: list[str] = []
    for h in hits:
        lines.append(
            f"  {local_date_time(h.get('ts_commit'))}  {h.get('kind'):<16} "
            f"{str(h.get('sha') or '')[:8]:<8}  {_first_line(h.get('text'), 96)}"
        )
        lines.append(f"      {h.get('uid')}")
    if not hits:
        lines.append("  no matches")
    return _emit(o, hits, lines)


def cmd_timeline(o: Opts) -> int:
    _, store = _store(o)
    rows = store.range(
        since=o.since, until=o.until, unit=o.unit, kinds=o.kinds, limit=o.limit
    )
    lines: list[str] = []
    for r in rows:
        who = r.get("author_agent") or r.get("author_human") or ""
        lines.append(
            f"  {local_date_time(r.get('ts_commit'))}  {r.get('unit'):<14} "
            f"{r.get('kind'):<16} {who:<12} {_first_line(r.get('text'), 80)}"
        )
    if not rows:
        lines.append("  no events in range")
    return _emit(o, rows, lines)


def cmd_show(o: Opts) -> int:
    _, store = _store(o)
    ev = store.event(o.uid)
    if ev is None:
        print(f"no such event: {o.uid}", file=sys.stderr)
        return 1
    # Neighbours scan the unit and sort in Python. Fine per-codebase; if this ever
    # matters, the fix is a range filter around ts_commit, not an index.
    siblings = store.range(unit=str(ev["unit"]), limit=0)
    idx = next((i for i, r in enumerate(siblings) if r["uid"] == ev["uid"]), -1)
    window = siblings[max(0, idx - 3) : idx + 4] if idx >= 0 else [ev]

    agent = f"{ev['author_agent']} {ev['author_model']}".strip()
    lines = [
        f"uid      {ev['uid']}",
        f"unit     {ev['unit']}",
        f"kind     {ev['kind']}",
        f"authored {local_date_time(ev['ts_author'])}",
        f"commit   {local_date_time(ev['ts_commit'])}",
        f"sha      {ev['sha']}",
        f"human    {ev['author_human']}",
        f"agent    {agent}",
        f"refs     {ev['refs']}",
        "",
        str(ev["text"]),
        "",
        "neighbours:",
    ]
    for r in window:
        marker = ">>" if r["uid"] == ev["uid"] else "  "
        lines.append(
            f"  {marker} {local_date_time(r['ts_commit'])}  "
            f"{r['kind']:<16} {_first_line(r.get('text'), 70)}"
        )

    return _emit(o, {"event": ev, "neighbours": window}, lines)


def cmd_exclude(o: Opts) -> int:
    """Not one of the five — a repair hatch for when the exclude line went missing."""
    root = _root(o)
    path = ensure_excluded(root)
    return _emit(o, {"root": root, "exclude": path}, [f"registered in {path}"])


# Typed dispatch, rather than argparse's `set_defaults(func=...)`. A Namespace
# attribute is `Any`, so calling it defeats the checker at exactly the point where the
# whole CLI surface is decided.
COMMANDS: dict[str, Callable[[Opts], int]] = {
    "index": cmd_index,
    "status": cmd_status,
    "search": cmd_search,
    "timeline": cmd_timeline,
    "show": cmd_show,
    "exclude": cmd_exclude,
}


# ---------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--path", default=argparse.SUPPRESS,
                        help="directory inside the codebase (default: cwd)")
    common.add_argument("--repo", default=argparse.SUPPRESS,
                        help="name a codebase in the ghq tree instead of using cwd")

    p = argparse.ArgumentParser(prog="cbo", parents=[common],
                                description="Timeline-as-truth index over one codebase.")
    sub = p.add_subparsers(dest="cmd", required=True)

    x = sub.add_parser("index", parents=[common], help="read git and gh into the store")
    x.add_argument("--no-gh", action="store_true", help="skip GitHub entirely")
    x.add_argument("--since", default="", help="only history after this date")
    x.add_argument("--full", action="store_true", help="ignore watermarks and re-read")

    sub.add_parser("status", parents=[common], help="units, counts, and staleness")

    x = sub.add_parser("search", parents=[common], help="full-text search the log")
    x.add_argument("query")
    x.add_argument("--unit", default="")
    x.add_argument("--limit", type=int, default=20)

    x = sub.add_parser("timeline", parents=[common], help="merged chronological view")
    x.add_argument("--since", default="")
    x.add_argument("--until", default="")
    x.add_argument("--unit", default="")
    x.add_argument("--kind", action="append")
    x.add_argument("--limit", type=int, default=100)

    x = sub.add_parser("show", parents=[common], help="one event and its neighbours")
    x.add_argument("uid")

    sub.add_parser("exclude", parents=[common],
                   help="re-register the store in .git/info/exclude")

    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return COMMANDS[str(a.cmd)](opts(a))
    except (GitError, RepoNotFound) as exc:
        print(f"{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
