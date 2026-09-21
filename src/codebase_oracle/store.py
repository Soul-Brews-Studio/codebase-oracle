"""LanceDB write and read path. Every comment here is a bug someone already paid for.

No compaction calls: the sibling index runs 6.01M events in 6.0 GB with none, and the
92 GB-file-holding-4 GB horror story was libSQL leaking DiskANN rebuilds into its
freelist, not Lance.

No `vectors` table, and that is a measured decision rather than a deferral. On 200
queries over 3,000 documents, FTS with ICU scored MRR 0.890 against 0.600 for
multilingual-e5-small; on Thai, lexical beat every vector model by 2.5x; RRF fusion
made things WORSE (0.890 to 0.822); and embedding the query cost 36.7s of a 37.4s
search. Commit subjects and issue titles are shorter and more known-item than prose,
so the gap only widens here.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from typing import Any

import lancedb

from .models import EVENTS, UNITS, WATERMARKS, EventRow, UnitRow, WatermarkRow

Row = dict[str, Any]


def sql_str(v: str) -> str:
    """Quote a value for a Lance filter. Filters are SQL strings, so this is required."""
    return v.replace("'", "''")


# Arrow type -> the SQL literal `add_columns` backfills with. The CAST is not optional:
# a bare `0` lands as int32 and the column then disagrees with the model, which is what
# the schema-vs-disk test exists to catch.
#
# These are Lance's OWN type names, not standard SQL. `VARCHAR` is rejected outright —
# "Unsupported data type: Varchar(None)" — and the accepted set is string / bigint / int /
# double / boolean / binary / date / timestamp / decimal.
_BACKFILL = {
    "string": "CAST('' AS string)",
    "large_string": "CAST('' AS string)",
    "int64": "CAST(0 AS bigint)",
    "int32": "CAST(0 AS int)",
    "double": "CAST(0.0 AS double)",
    "bool": "CAST(false AS boolean)",
}


def _widen(table: Any, model: Any) -> list[str]:
    """Add columns the model has and the table does not. Returns what was added.

    `merge_insert` does NOT auto-widen — it rejects the whole batch with
    `Field '<name>' not found in target schema`. So a new field makes every subsequent
    write fail, including `index --full`, which cannot repair it either because it also
    merges into the existing table. Without this the only recovery is deleting the store.

    Widening never removes or retypes anything, so it cannot lose data. A column the
    table has and the model lost is left alone, and the schema test flags that separately
    as the model being behind disk.
    """
    have = {f.name for f in table.schema}
    added: list[str] = []
    for field in model.to_arrow_schema():
        if field.name in have:
            continue
        literal = _BACKFILL.get(str(field.type))
        if literal is None:
            raise RuntimeError(
                f"cannot widen {field.name}: no backfill literal for {field.type}"
            )
        table.add_columns({field.name: literal})
        added.append(field.name)
    return added


def _rows(query: Any) -> list[Row]:
    """Materialise an untyped Lance query into plain dicts.

    One conversion point, for the same reason there is one clock: a result that stays
    `Any` spreads silently through every caller, and then the checker is only pretending
    to check them.
    """
    return [dict(r) for r in query.to_list()]


class Store:
    def __init__(self, dirname: str) -> None:
        os.makedirs(dirname, exist_ok=True)
        self.dirname = dirname
        self.db = lancedb.connect(dirname)

    # --------------------------------------------------------------- read side

    def _existing(self, name: str) -> Any:
        """The table, or None. NEVER creates.

        Returns `Any` because lancedb ships no type information. This annotation is the
        declared edge of the typed world: everything a table hands back is converted at
        the boundary (see `_rows`), so `Any` does not leak past this class.

        Two traps in one call. `list_tables()` returns a ListTablesResponse, not a
        list of strings, so `name not in self.db.list_tables()` is False for every
        table that exists and the whole index silently reports zero rows. Read
        `.tables` explicitly. The deprecated `table_names()` is not a safe substitute
        either — it defaults to `limit=10`.

        Not creating on read is also deliberate: materialising an empty `events` turns
        "nothing has been indexed" into "indexed, zero rows", which looks healthy in
        status output.
        """
        try:
            if name not in self.db.list_tables().tables:
                return None
            return self.db.open_table(name)
        except Exception:
            return None

    def has_index(self) -> bool:
        return self._existing(EVENTS) is not None

    def count_events(self, where: str | None = None) -> int:
        t = self._existing(EVENTS)
        if t is None:
            return 0
        try:
            return int(t.count_rows(where) if where else t.count_rows())
        except Exception:
            return 0

    def counts_by_kind(self) -> dict[str, int]:
        t = self._existing(EVENTS)
        if t is None:
            return {}
        out: dict[str, int] = {}
        for row in _rows(t.search().select(["kind"]).limit(0)):
            out[row["kind"]] = out.get(row["kind"], 0) + 1
        return out

    def units(self) -> list[Row]:
        t = self._existing(UNITS)
        if t is None:
            return []
        return sorted(_rows(t.search().limit(0)), key=lambda r: str(r["unit"]))

    def watermark(self, unit: str, source: str) -> Row | None:
        t = self._existing(WATERMARKS)
        if t is None:
            return None
        key = sql_str(f"{unit}:{source}")
        rows = _rows(t.search().where(f"key = '{key}'").limit(2))
        return rows[0] if rows else None

    def event(self, uid: str) -> Row | None:
        t = self._existing(EVENTS)
        if t is None:
            return None
        rows = _rows(t.search().where(f"uid = '{sql_str(uid)}'").limit(2))
        return rows[0] if rows else None

    def range(
        self,
        since: str = "",
        until: str = "",
        unit: str = "",
        kinds: Sequence[str] = (),
        limit: int = 200,
    ) -> list[Row]:
        """Events in a window, chronological.

        String comparison on `ts_commit` is valid only because time.to_utc_iso
        normalises every stamp to +00:00 — that is what makes the column lexically
        sortable and lets the range live in the filter instead of in Python.
        """
        t = self._existing(EVENTS)
        if t is None:
            return []
        clauses: list[str] = []
        if since:
            clauses.append(f"ts_commit >= '{sql_str(since)}'")
        if until:
            clauses.append(f"ts_commit <= '{sql_str(until)}'")
        if unit:
            clauses.append(f"unit = '{sql_str(unit)}'")
        if kinds:
            inner = ", ".join(f"'{sql_str(k)}'" for k in kinds)
            clauses.append(f"kind IN ({inner})")
        q = t.search()
        if clauses:
            q = q.where(" AND ".join(clauses))
        rows = _rows(q.limit(0))
        rows.sort(key=lambda r: (r.get("ts_commit") or "", r.get("uid") or ""))
        return rows[:limit] if limit else rows

    def search(self, query: str, limit: int = 20, where: str = "") -> list[Row]:
        """FTS, falling back to LIKE.

        A shard whose FTS index failed to build must still answer. The fallback is
        7-28x slower and carries no `_score`, which is strictly better than a silent
        empty result that looks like "no such thing exists".
        """
        t = self._existing(EVENTS)
        if t is None:
            return []
        try:
            q = t.search(query, query_type="fts")
            if where:
                q = q.where(where)
            return _rows(q.limit(limit))
        except Exception:
            q = t.search().where(
                f"text LIKE '%{sql_str(query)}%'" + (f" AND ({where})" if where else "")
            )
            return _rows(q.limit(limit))

    # -------------------------------------------------------------- write side

    def _merge(self, name: str, model: Any, key: str, rows: Iterable[Any]) -> int:
        """Upsert by `key`, creating the table on first write.

        merge_insert REJECTS THE WHOLE BATCH when two source rows share a key
        ("Ambiguous merge inserts are prohibited") — not the offending row, the entire
        batch. That is how a sibling index silently dropped 4,439 notes: per-file
        writes could never collide, and 250-file batches could. Dedupe here, keeping
        the LAST occurrence, so one bad pair cannot discard a whole run.

        Exceptions are NOT swallowed on this path. A write that fails must be loud.
        """
        data = [r.model_dump() for r in rows]
        if not data:
            return 0
        seen: dict[str, Row] = {}
        for d in data:
            seen[d[key]] = d
        data = list(seen.values())
        t = self._existing(name)
        if t is None:
            self.db.create_table(name, data=data, schema=model.to_arrow_schema())
            return len(data)
        _widen(t, model)
        (
            t.merge_insert(key)
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute(data)
        )
        return len(data)

    def put_events(self, rows: Iterable[EventRow]) -> int:
        return self._merge(EVENTS, EventRow, "uid", rows)

    def put_units(self, rows: Iterable[UnitRow]) -> int:
        return self._merge(UNITS, UnitRow, "unit", rows)

    def put_watermarks(self, rows: Iterable[WatermarkRow]) -> int:
        return self._merge(WATERMARKS, WatermarkRow, "key", rows)

    def delete_unit_events(self, unit: str, kinds: Sequence[str]) -> None:
        """Drop one unit's rows for the named kinds, before that source is re-read.

        `kinds` is required, not optional. Deleting a whole unit meant `--full --no-gh`
        cleared the GitHub transitions and then re-read only git, silently destroying
        data the run never intended to touch — observed for real on a repo that had 233
        indexed transitions. A full re-read must only clear what it is about to rewrite.
        """
        t = self._existing(EVENTS)
        if t is None or not kinds:
            return
        inner = ", ".join(f"'{sql_str(k)}'" for k in kinds)
        t.delete(f"unit = '{sql_str(unit)}' AND kind IN ({inner})")

    def ensure_fts_index(self) -> None:
        t = self._existing(EVENTS)
        if t is None:
            return
        for idx in t.list_indices():
            if idx.index_type == "FTS":
                return
        # Every argument below is measured, not taste.
        #
        # use_tantivy=False — native Lance FTS. Tantivy is the older path and does not
        # take the ICU tokenizer.
        #
        # base_tokenizer="icu" — real Thai word segmentation. Thai has no spaces, so
        # `simple` turns an entire Thai sentence into ONE token: measured 1 of 12
        # substrings findable against ICU's 11. ngram(3) misses 2-character queries.
        #
        # stem=False — this is a code corpus and the English stemmer mangles
        # identifiers: structured_output_mode becomes structured_output_mod.
        #
        # max_token_length=128 — 40-char shas and long identifiers survive whole.
        t.create_fts_index(
            "text",
            use_tantivy=False,
            base_tokenizer="icu",
            stem=False,
            remove_stop_words=False,
            max_token_length=128,
        )
