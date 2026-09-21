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

# Edge vocabulary. PROV-O names where the standard has one — they are peer-reviewed and
# unambiguous, and give free alignment if this is ever exported — plain verbs otherwise.
EdgeKind = Literal[
    "wasDerivedFrom",   # commit -> parent commit
    "touched",          # commit -> file
    "bumped",           # commit -> submodule range
    "resolves",         # commit -> issue, GitHub itself resolved the keyword
    "closes",           # commit -> issue, the AUTHOR claimed closure. A claim, not a fact
    "merged_as",        # pr -> commit
    "mentions",         # commit -> issue, a bare "#N" appeared
    "wasAttributedTo",  # commit -> agent
]

# HOW an edge was derived, which is the only honest form of confidence. A float would be
# decoration: nothing consumes it, and it goes stale the day the extraction improves.
# Precision is a property of the method, so the method is what gets recorded.
EdgeSource = Literal[
    "parent",         # exact — from the commit's own parent list
    "file-change",    # exact — from a file-change row
    "gitlink",        # exact — from a gitlink diff
    "gh-closed",      # GitHub resolved a closing keyword
    "gh-merged",      # GitHub recorded the merge
    "gh-referenced",  # GitHub resolved a cross-reference — a real link, not a closure
    "trailer",        # Co-Authored-By
    "regex-keyword",  # the author WROTE "closes #N" — reliable as intent, not as outcome
    "regex-hash",     # ~0.60 precision — a bare "#N" we scraped, must be discounted
]

# Sources reliable enough that the LINK itself can be believed. Everything here was
# resolved by GitHub or is structurally exact; `regex-*` is our own scraping, which finds
# at most half the real links at ~0.60 precision.
#
# This is about link reliability, NOT about closure. A `gh-referenced` edge is a genuine
# reference and still says nothing about whether an issue closed — which is why
# `contradictions` keys off the `closes` edge KIND rather than off trust.
TRUSTED_SOURCES: tuple[str, ...] = (
    "parent",
    "file-change",
    "gitlink",
    "gh-closed",
    "gh-merged",
    "gh-referenced",
    "trailer",
)

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
    # Space-joined parent shas on commit events. APPENDED rather than carved out of
    # from_sha, which used to hold both parents and gitlink targets — one column, two
    # meanings. Renaming a field does not raise; it silently creates a second column and
    # leaves the old one holding the only data there was. So from_sha keeps its name and
    # its gitlink meaning, and commits get their own column.
    parents: str
    # For a gh event whose commit_id belongs to a DIFFERENT repository — a
    # `referenced` transition can name a sha this codebase has never seen. Empty when
    # the sha is local. Without it a cross-repo edge points at a node that cannot exist.
    sha_repo: str
    # The GitHub timeline event name: closed, merged, referenced, commented, labeled…
    # `kind` only says issue-transition vs pr-transition, and edge derivation needs the
    # distinction: `closed` carrying a commit_id is GitHub resolving a keyword, while
    # `referenced` carrying one is merely a mention. Its own column rather than parsed
    # back out of `text`.
    gh_event: str


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
