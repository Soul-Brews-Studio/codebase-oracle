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
from typing import Any, Iterable, Sequence

import lancedb

from .models import EVENTS, UNITS, WATERMARKS, EventRow, UnitRow, WatermarkRow


def sql_str(v: str) -> str:
    """Quote a value for a Lance filter. Filters are SQL strings, so this is required."""
    return v.replace("'", "''")


class Store:
    def __init__(self, dirname: str) -> None:
        os.makedirs(dirname, exist_ok=True)
        self.dirname = dirname
        self.db = lancedb.connect(dirname)

    # --------------------------------------------------------------- read side

    def _existing(self, name: str):
        """The table, or None. NEVER creates.

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
            return t.count_rows(where) if where else t.count_rows()
        except Exception:
            return 0

    def counts_by_kind(self) -> dict[str, int]:
        t = self._existing(EVENTS)
        if t is None:
            return {}
        out: dict[str, int] = {}
        for row in t.search().select(["kind"]).limit(0).to_list():
            out[row["kind"]] = out.get(row["kind"], 0) + 1
        return out

    def units(self) -> list[dict]:
        t = self._existing(UNITS)
        if t is None:
            return []
        return sorted(t.search().limit(0).to_list(), key=lambda r: r["unit"])

    def watermark(self, unit: str, source: str) -> dict | None:
        t = self._existing(WATERMARKS)
        if t is None:
            return None
        key = sql_str(f"{unit}:{source}")
        rows = t.search().where(f"key = '{key}'").limit(2).to_list()
        return rows[0] if rows else None

    def event(self, uid: str) -> dict | None:
        t = self._existing(EVENTS)
        if t is None:
            return None
        rows = t.search().where(f"uid = '{sql_str(uid)}'").limit(2).to_list()
        return rows[0] if rows else None

    def range(
        self,
        since: str = "",
        until: str = "",
        unit: str = "",
        kinds: Sequence[str] = (),
        limit: int = 200,
    ) -> list[dict]:
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
        rows = q.limit(0).to_list()
        rows.sort(key=lambda r: (r.get("ts_commit") or "", r.get("uid") or ""))
        return rows[:limit] if limit else rows

    def search(self, query: str, limit: int = 20, where: str = "") -> list[dict]:
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
            return q.limit(limit).to_list()
        except Exception:
            q = t.search().where(
                f"text LIKE '%{sql_str(query)}%'" + (f" AND ({where})" if where else "")
            )
            return q.limit(limit).to_list()

    # -------------------------------------------------------------- write side

    def _merge(self, name: str, model: Any, key: str, rows: Iterable) -> int:
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
        seen: dict[str, dict] = {}
        for d in data:
            seen[d[key]] = d
        data = list(seen.values())
        t = self._existing(name)
        if t is None:
            self.db.create_table(name, data=data, schema=model.to_arrow_schema())
            return len(data)
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

    def delete_unit_events(self, unit: str) -> None:
        """Drop a unit's rows before a full re-read, or two generations coexist."""
        t = self._existing(EVENTS)
        if t is None:
            return
        t.delete(f"unit = '{sql_str(unit)}'")

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
