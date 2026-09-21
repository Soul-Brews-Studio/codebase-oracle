"""cbo — five commands over one codebase's event log.

The `common` parent parser with `default=argparse.SUPPRESS` is load-bearing, not
decoration. Without a parent, `cbo status --json` exits 2 because the subparser has
never heard of `--json`. With an ordinary default on the parent, the subparser's default
overwrites whatever the top-level flag set. SUPPRESS means an absent flag sets no
attribute at all, so `getattr(a, "json", False)` is the only correct way to read one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .gitio import GitError
from .indexer import ensure_excluded, run_index, store_dir
from .store import Store, sql_str
from .time import local_date_time, zone_offset
from .units import RepoNotFound, codebase_root, resolve_repo


def _json(a) -> bool:
    return getattr(a, "json", False)


def _root(a) -> str:
    """Which codebase this invocation is about.

    `--repo <name>` exists because the common question is asked from somewhere else:
    sitting in one repo and wanting to know what happened in another. It resolves a name
    through the ghq tree and then opens THAT codebase's own store — there is still no
    global index, only a different one.
    """
    repo = getattr(a, "repo", "") or ""
    if repo:
        return codebase_root(resolve_repo(repo))
    return codebase_root(getattr(a, "path", None) or os.getcwd())


def _store(a) -> tuple[str, Store]:
    root = _root(a)
    return root, Store(store_dir(root))


def _out(a, payload, lines: list[str]) -> int:
    if _json(a):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for line in lines:
            print(line)
    return 0


# ------------------------------------------------------------------- commands


def cmd_index(a) -> int:
    root = _root(a)
    summary = run_index(
        root,
        with_gh=not getattr(a, "no_gh", False),
        since=getattr(a, "since", "") or "",
        full=getattr(a, "full", False),
    )
    if _json(a):
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0
    for u in summary["units"]:
        mark = "" if u["indexed"] else "  (not initialised — bumps only)"
        print(f"  {u['unit']:<28} +{u['written']}{mark}")
    for err in summary["gh_errors"]:
        print(f"  gh skipped for {err['repo']}: {err['error']}", file=sys.stderr)
    return 0


def cmd_status(a) -> int:
    root, store = _store(a)
    if not store.has_index():
        # Deliberately distinct from "indexed, zero events" — see Store._existing.
        print(f"no index at {store_dir(root)} — run: cbo index", file=sys.stderr)
        return 1

    units = store.units()
    kinds = store.counts_by_kind()
    rows = []
    lines = [f"codebase  {root}", f"store     {store_dir(root)}", ""]

    for u in units:
        wm = store.watermark(u["unit"], "git") or {}
        stale = ""
        if u["indexed"]:
            try:
                from .gitio import git

                head = git(u["path"], "rev-parse", "HEAD", check=False)
            except GitError:
                head = ""
            if head and wm.get("last_sha") and head != wm["last_sha"]:
                stale = f"  STALE (HEAD {head[:8]} != indexed {wm['last_sha'][:8]})"
            elif not head:
                stale = "  unreadable"
        else:
            stale = "  not initialised"
        rows.append({**u, "stale": bool(stale.strip())})
        lines.append(f"  {u['unit']:<28} {u['event_count']:>7} events  {u['url']}{stale}")

    lines.append("")
    for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {k:<20} {n:>7}")
    lines.append("")
    lines.append(f"  times shown in local time ({zone_offset()}); stored UTC")

    return _out(a, {"root": root, "units": rows, "kinds": kinds}, lines)


def cmd_search(a) -> int:
    _root_, store = _store(a)
    where = f"unit = '{sql_str(a.unit)}'" if getattr(a, "unit", "") else ""
    hits = store.search(a.query, limit=a.limit, where=where)
    lines = []
    for h in hits:
        first = (h.get("text") or "").strip().split("\n")[0][:96]
        lines.append(
            f"  {local_date_time(h.get('ts_commit'))}  {h.get('kind'):<16} "
            f"{(h.get('sha') or '')[:8]:<8}  {first}"
        )
        lines.append(f"      {h.get('uid')}")
    if not hits:
        lines.append("  no matches")
    return _out(a, hits, lines)


def cmd_timeline(a) -> int:
    _root_, store = _store(a)
    kinds = tuple(a.kind) if getattr(a, "kind", None) else ()
    rows = store.range(
        since=getattr(a, "since", "") or "",
        until=getattr(a, "until", "") or "",
        unit=getattr(a, "unit", "") or "",
        kinds=kinds,
        limit=a.limit,
    )
    lines = []
    for r in rows:
        who = r.get("author_agent") or r.get("author_human") or ""
        first = (r.get("text") or "").strip().split("\n")[0][:80]
        lines.append(
            f"  {local_date_time(r.get('ts_commit'))}  {r.get('unit'):<14} "
            f"{r.get('kind'):<16} {who:<12} {first}"
        )
    if not rows:
        lines.append("  no events in range")
    return _out(a, rows, lines)


def cmd_show(a) -> int:
    _root_, store = _store(a)
    ev = store.event(a.uid)
    if ev is None:
        print(f"no such event: {a.uid}", file=sys.stderr)
        return 1
    # Neighbours scan the unit and sort in Python. Fine per-codebase; if this ever
    # matters, the fix is a range filter around ts_commit, not an index.
    siblings = store.range(unit=ev["unit"], limit=0)
    idx = next((i for i, r in enumerate(siblings) if r["uid"] == ev["uid"]), -1)
    window = siblings[max(0, idx - 3) : idx + 4] if idx >= 0 else [ev]

    lines = [
        f"uid      {ev['uid']}",
        f"unit     {ev['unit']}",
        f"kind     {ev['kind']}",
        f"authored {local_date_time(ev['ts_author'])}",
        f"commit   {local_date_time(ev['ts_commit'])}",
        f"sha      {ev['sha']}",
        f"human    {ev['author_human']}",
        f"agent    {ev['author_agent']} {ev['author_model']}".rstrip(),
        f"refs     {ev['refs']}",
        "",
        ev["text"],
        "",
        "neighbours:",
    ]
    for r in window:
        marker = ">>" if r["uid"] == ev["uid"] else "  "
        first = (r.get("text") or "").strip().split("\n")[0][:70]
        lines.append(f"  {marker} {local_date_time(r['ts_commit'])}  {r['kind']:<16} {first}")

    return _out(a, {"event": ev, "neighbours": window}, lines)


def cmd_exclude(a) -> int:
    """Not in the five — a repair hatch for when the exclude line went missing."""
    root = _root(a)
    path = ensure_excluded(root)
    return _out(a, {"root": root, "exclude": path}, [f"registered in {path}"])


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
    x.set_defaults(func=cmd_index)

    x = sub.add_parser("status", parents=[common], help="units, counts, and staleness")
    x.set_defaults(func=cmd_status)

    x = sub.add_parser("search", parents=[common], help="full-text search the log")
    x.add_argument("query")
    x.add_argument("--unit", default="")
    x.add_argument("--limit", type=int, default=20)
    x.set_defaults(func=cmd_search)

    x = sub.add_parser("timeline", parents=[common], help="merged chronological view")
    x.add_argument("--since", default="")
    x.add_argument("--until", default="")
    x.add_argument("--unit", default="")
    x.add_argument("--kind", action="append")
    x.add_argument("--limit", type=int, default=100)
    x.set_defaults(func=cmd_timeline)

    x = sub.add_parser("show", parents=[common], help="one event and its neighbours")
    x.add_argument("uid")
    x.set_defaults(func=cmd_show)

    x = sub.add_parser("exclude", parents=[common], help="re-register the store in .git/info/exclude")
    x.set_defaults(func=cmd_exclude)

    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.func(a)
    except (GitError, RepoNotFound) as exc:
        print(f"{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
