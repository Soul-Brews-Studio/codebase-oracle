"""The model IS the schema. Arrow is derived from these type hints, never hand-written.

Column names are wire format. Renaming a field does not raise — it creates a SECOND
column and leaves the old one full of the only data you had.

Integers are declared `int`, which is worth stating because the transcript indexer
beside us declares `float` for the same concept. That is not a style difference: its
tables were created by a TypeScript writer, JavaScript has one number type, so `seq`
and friends are float64 on disk forever. An `int` hint does not merge into a float64
column — the write fails, or worse lands a duplicate column. These tables are
greenfield and Python-owned, so int64 is correct here and nothing forces the lie.

The inversion matters later: when the v2 session join reads THAT index, it must
expect float64.
"""

from __future__ import annotations

from typing import Literal

from lancedb.pydantic import LanceModel
from pydantic import BaseModel

# Five kinds, and the set is closed. `submodule-bump` is the cross-unit join: it is
# how a superproject commit names the upstream range it pulled in.
Kind = Literal[
    "commit",
    "file-change",
    "submodule-bump",
    "issue-transition",
    "pr-transition",
]

EVENTS = "events"
UNITS = "units"
WATERMARKS = "watermarks"

# Which kinds each source owns. A full re-read clears only its own source's kinds, so
# re-indexing git cannot delete GitHub transitions it is not going to rewrite.
GIT_KINDS: tuple[str, ...] = ("commit", "file-change", "submodule-bump")
GH_KINDS: tuple[str, ...] = ("issue-transition", "pr-transition")

STORE_DIRNAME = ".codebase-oracle"


class EventRow(LanceModel):
    """One thing that happened. Append-only; current state is a fold over these.

    Ordering is (ts_commit, uid) and there is deliberately no `seq` column. Any
    sequence number cheap enough to compute per commit — rev-list position, for
    instance — shifts when history grows, and an unstable ordering key is precisely
    the class of bug this project exists to expose.
    """

    uid: str
    unit: str
    kind: str
    # Both git dates. Rebase and cherry-pick leave them different on purpose, so
    # neither is derived from the other. UTC ISO-8601, see time.to_utc_iso.
    ts_author: str
    ts_commit: str
    sha: str
    path: str
    # The FTS column: commit message, or issue/PR/comment body.
    text: str
    # Three authors, because one is a lie. git reports the committer's configured
    # identity for every commit an agent wrote; the model appears only in the
    # Co-Authored-By trailer. For gh events, author_human is the acting login.
    author_human: str
    author_agent: str
    author_model: str
    # Space-joined issue/PR references found in the message ("#43 #44"), kept as text
    # so FTS can reach them.
    refs: str
    number: int
    insertions: int
    deletions: int
    # submodule-bump only: the gitlink's old and new target.
    from_sha: str
    to_sha: str


class UnitRow(LanceModel):
    """One git history inside this codebase: the superproject, or one submodule.

    `indexed` is load-bearing. An uninitialised submodule has no local objects, so we
    can see its bumps from the superproject but cannot read its commits. Reporting
    that as zero events would be indistinguishable from an empty repository.
    """

    unit: str
    path: str
    url: str
    head: str
    indexed: bool
    event_count: int
    indexed_at: str


class WatermarkRow(LanceModel):
    """How far each (unit, source) pair has been read.

    Purely an optimisation. Ids are content-addressed, so a lost or corrupt watermark
    costs time on the next run and cannot corrupt the table.
    """

    key: str
    unit: str
    source: str
    last_sha: str
    last_cursor: str
    updated_at: str


class Unit(BaseModel):
    """Discovery-time view of a unit, before anything is written."""

    unit: str
    abs_path: str
    url: str = ""
    head: str = ""
    initialised: bool = True
