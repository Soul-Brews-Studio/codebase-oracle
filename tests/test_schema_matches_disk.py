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

import pytest

lancedb = pytest.importorskip("lancedb")

from codebase_oracle.models import EVENTS, UNITS, WATERMARKS, EventRow, UnitRow, WatermarkRow
from codebase_oracle.indexer import store_dir
from codebase_oracle.units import codebase_root

MODELS = {EVENTS: EventRow, UNITS: UnitRow, WATERMARKS: WatermarkRow}

# Fields the model has and disk does not, pending a lazy widen on next write.
PENDING_MIGRATION: dict[str, set[str]] = {}


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
def test_model_is_not_behind_disk(table):
    disk = _disk(table)
    mine = {f.name: str(f.type) for f in MODELS[table].to_arrow_schema()}
    missing = set(disk) - set(mine)
    assert not missing, f"{table}: disk has columns the model lost: {missing}"


@pytest.mark.parametrize("table", sorted(MODELS))
def test_model_ahead_of_disk_is_declared(table):
    disk = _disk(table)
    mine = {f.name: str(f.type) for f in MODELS[table].to_arrow_schema()}
    ahead = set(mine) - set(disk)
    allowed = PENDING_MIGRATION.get(table, set())
    assert ahead <= allowed, f"{table}: undeclared new columns: {ahead - allowed}"


@pytest.mark.parametrize("table", sorted(MODELS))
def test_types_match_on_shared_columns(table):
    disk = _disk(table)
    mine = {f.name: str(f.type) for f in MODELS[table].to_arrow_schema()}
    for name in set(disk) & set(mine):
        assert disk[name] == mine[name], f"{table}.{name}: disk {disk[name]} vs model {mine[name]}"
