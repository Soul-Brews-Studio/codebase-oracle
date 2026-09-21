"""The model may be AHEAD of disk, never BEHIND.

Behind disk means a rewrite would silently drop a column that holds the only copy of
some data. Ahead of disk is a pending migration and is fine — but only for fields named
explicitly in PENDING_MIGRATION, so that a typo'd field name still fails.

The type comparison runs over the INTERSECTION only. That is the test that catches an
`int` hint meeting a float64 column, which is the exact failure mode waiting at the v2
boundary where this reads the transcript index's tables.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

lancedb = pytest.importorskip("lancedb")

# E402 is correct in general and wrong here: these imports pull in lancedb transitively,
# so hoisting them above importorskip turns a clean skip on a machine without lancedb
# into a collection error.
from codebase_oracle.indexer import store_dir  # noqa: E402
from codebase_oracle.models import (  # noqa: E402
    EVENTS,
    UNITS,
    WATERMARKS,
    EventRow,
    UnitRow,
    WatermarkRow,
)
from codebase_oracle.units import codebase_root  # noqa: E402

# Any, not type[LanceModel]: lancedb ships no stubs, so mypy resolves the row classes
# through pydantic's ModelMetaclass and cannot see to_arrow_schema on them.
MODELS: dict[str, Any] = {EVENTS: EventRow, UNITS: UnitRow, WATERMARKS: WatermarkRow}

# Fields the model has and disk does not, pending a lazy widen on next write.
#
# These three landed with the graph work and are ahead of any store written before it.
# `parents` split the commit DAG out of `from_sha`, which meant both parent shas and
# gitlink targets; `sha_repo` marks a commit_id belonging to another repository;
# `gh_event` keeps the timeline event name that edge derivation needs. A re-index with
# `--full` brings disk level again, at which point these can be removed.
PENDING_MIGRATION: dict[str, set[str]] = {
    EVENTS: {"parents", "sha_repo", "gh_event"},
}


def _disk(table: str) -> dict[str, str]:
    root = codebase_root(os.path.dirname(os.path.abspath(__file__)))
    d = store_dir(root)
    if not os.path.isdir(d):
        pytest.skip(f"no index at {d} — run `cbo index` first")
    db = lancedb.connect(d)
    if table not in db.list_tables().tables:
        pytest.skip(f"table {table} not written yet")
    return {f.name: str(f.type) for f in db.open_table(table).schema}


@pytest.mark.parametrize("table", sorted(MODELS))
def test_model_is_not_behind_disk(table: str) -> None:
    disk = _disk(table)
    mine = {f.name: str(f.type) for f in MODELS[table].to_arrow_schema()}
    missing = set(disk) - set(mine)
    assert not missing, f"{table}: disk has columns the model lost: {missing}"


@pytest.mark.parametrize("table", sorted(MODELS))
def test_model_ahead_of_disk_is_declared(table: str) -> None:
    disk = _disk(table)
    mine = {f.name: str(f.type) for f in MODELS[table].to_arrow_schema()}
    ahead = set(mine) - set(disk)
    allowed = PENDING_MIGRATION.get(table, set())
    assert ahead <= allowed, f"{table}: undeclared new columns: {ahead - allowed}"


@pytest.mark.parametrize("table", sorted(MODELS))
def test_types_match_on_shared_columns(table: str) -> None:
    disk = _disk(table)
    mine = {f.name: str(f.type) for f in MODELS[table].to_arrow_schema()}
    for name in set(disk) & set(mine):
        assert disk[name] == mine[name], f"{table}.{name}: disk {disk[name]} vs model {mine[name]}"
