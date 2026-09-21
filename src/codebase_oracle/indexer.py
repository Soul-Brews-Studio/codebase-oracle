"""Orchestration: units in, events out, watermarks updated.

Progress reporting here obeys one rule learned the hard way: a counter must count what
it claims. A sibling indexer counted IMPORTED rather than SCANNED files and reported a
17-hour ETA for a run that finished in 250 seconds, which twice got diagnosed as a hung
process. And progress is written with a newline rather than a carriage return whenever
stdout is not a TTY, because `\\r` through a pipe produces no output at all until EOF.
"""

from __future__ import annotations

import os
import sys
from typing import Callable

from .gitio import git
from .models import STORE_DIRNAME, Unit, UnitRow, WatermarkRow
from .sources import gh as gh_source
from .sources import git as git_source
from .store import Store
from .time import now_utc_iso
from .units import discover

Progress = Callable[[str], None]


def store_dir(root: str) -> str:
    return os.path.join(root, STORE_DIRNAME)


def ensure_excluded(root: str) -> str:
    """Keep the store out of the indexed repo's git status, without touching its
    committed .gitignore.

    This tool indexes repositories it does not own, so editing their tracked
    `.gitignore` is off the table. `.git/info/exclude` is machine-local, never
    committed, and ripgrep honours it too — so the store stays invisible to both git and
    search.

    `--git-common-dir`, not `.git`: in a linked worktree `.git` is a FILE and the real
    admin directory (with info/exclude in it) is shared with the main checkout.
    """
    common = git(root, "rev-parse", "--git-common-dir", check=False)
    if not common:
        return ""
    if not os.path.isabs(common):
        common = os.path.join(root, common)
    info = os.path.join(common, "info")
    os.makedirs(info, exist_ok=True)
    path = os.path.join(info, "exclude")
    pattern = f"{STORE_DIRNAME}/"
    existing = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            existing = fh.read()
    if pattern in existing.split("\n") or STORE_DIRNAME in existing.split("\n"):
        return path
    with open(path, "a", encoding="utf-8") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write(pattern + "\n")
    return path


def default_progress(msg: str) -> None:
    end = "\r" if sys.stdout.isatty() else "\n"
    sys.stdout.write(msg.ljust(72) + end)
    sys.stdout.flush()


def run_index(
    root: str,
    *,
    with_gh: bool = True,
    since: str = "",
    full: bool = False,
    progress: Progress = default_progress,
) -> dict:
    """Index every unit of the codebase at `root`. Returns a summary dict."""
    ensure_excluded(root)
    store = Store(store_dir(root))
    units = discover(root)

    summary: dict = {"root": root, "units": [], "events": 0, "gh_errors": []}
    total = len(units)

    for i, unit in enumerate(units, 1):
        # Counts SCANNED units, which is what the denominator describes.
        progress(f"[{i}/{total}] {unit.unit}")
        written = 0

        if unit.initialised:
            if full:
                store.delete_unit_events(unit.unit)
            wm = None if full else store.watermark(unit.unit, "git")
            last_sha = (wm or {}).get("last_sha", "") or ""
            rows = list(git_source.read(unit.unit, unit.abs_path, last_sha, since))
            written += store.put_events(rows)
            head = git_source.head_sha(unit.abs_path)
            store.put_watermarks(
                [
                    WatermarkRow(
                        key=f"{unit.unit}:git",
                        unit=unit.unit,
                        source="git",
                        last_sha=head,
                        last_cursor="",
                        updated_at=now_utc_iso(),
                    )
                ]
            )
        else:
            head = unit.head

        if with_gh and unit.initialised and unit.url:
            written += _index_gh(store, unit, since, full, summary)

        store.put_units(
            [
                UnitRow(
                    unit=unit.unit,
                    path=unit.abs_path,
                    url=unit.url,
                    head=head,
                    indexed=unit.initialised,
                    event_count=store.count_events(f"unit = '{unit.unit}'"),
                    indexed_at=now_utc_iso(),
                )
            ]
        )
        summary["units"].append({"unit": unit.unit, "written": written, "indexed": unit.initialised})
        summary["events"] += written

    store.ensure_fts_index()
    progress(f"indexed {summary['events']} events across {total} unit(s)")
    if sys.stdout.isatty():
        sys.stdout.write("\n")
    return summary


def _index_gh(store: Store, unit: Unit, since: str, full: bool, summary: dict) -> int:
    wm = None if full else store.watermark(unit.unit, "gh")
    cursor = (wm or {}).get("last_cursor", "") or since
    try:
        rows = list(gh_source.read(unit.unit, unit.url, cursor))
    except gh_source.GhUnavailable as exc:
        # A missing or rate-limited gh must not fail the git half of the run. Record it
        # so `status` can say the gh side is stale rather than empty.
        summary["gh_errors"].append({"unit": unit.unit, "repo": unit.url, "error": str(exc)})
        return 0
    written = store.put_events(rows)
    store.put_watermarks(
        [
            WatermarkRow(
                key=f"{unit.unit}:gh",
                unit=unit.unit,
                source="gh",
                last_sha="",
                last_cursor=now_utc_iso(),
                updated_at=now_utc_iso(),
            )
        ]
    )
    return written
