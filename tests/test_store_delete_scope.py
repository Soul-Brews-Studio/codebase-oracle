"""A full re-read must clear only the source it is about to rewrite.

`--full --no-gh` used to delete a unit's GitHub transitions and then re-read git alone,
destroying 233 indexed transitions on a real repo. Nothing errored and the counts simply
got smaller, which is the worst shape a data-loss bug can take.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("lancedb")

from codebase_oracle.models import GH_KINDS, GIT_KINDS, EventRow
from codebase_oracle.store import Store


def _ev(uid: str, kind: str, unit: str = ".") -> EventRow:
    return EventRow(
        uid=uid, unit=unit, kind=kind,
        ts_author="2026-09-21T00:00:00+00:00", ts_commit="2026-09-21T00:00:00+00:00",
        sha="abc", path="", text=uid,
        author_human="Nat", author_agent="", author_model="",
        refs="", number=0, insertions=0, deletions=0, from_sha="", to_sha="",
    )


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(str(tmp_path / "store"))
    s.put_events([
        _ev("g1", "commit"),
        _ev("g2", "file-change"),
        _ev("g3", "submodule-bump"),
        _ev("h1", "issue-transition"),
        _ev("h2", "pr-transition"),
        _ev("other", "commit", unit="vendor/x"),
    ])
    return s


def test_clearing_git_kinds_leaves_github_events_alone(store: Store) -> None:
    store.delete_unit_events(".", GIT_KINDS)
    remaining = {r["uid"] for r in store.range(limit=0)}
    assert remaining == {"h1", "h2", "other"}


def test_clearing_github_kinds_leaves_git_events_alone(store: Store) -> None:
    store.delete_unit_events(".", GH_KINDS)
    remaining = {r["uid"] for r in store.range(limit=0)}
    assert remaining == {"g1", "g2", "g3", "other"}


def test_deletion_is_scoped_to_one_unit(store: Store) -> None:
    """A submodule's history must survive the superproject being re-read."""
    store.delete_unit_events(".", GIT_KINDS + GH_KINDS)
    assert {r["uid"] for r in store.range(limit=0)} == {"other"}


def test_empty_kinds_deletes_nothing(store: Store) -> None:
    """Guards against a future caller passing an empty tuple and wiping the unit."""
    store.delete_unit_events(".", ())
    assert len(store.range(limit=0)) == 6
