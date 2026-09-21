"""Content-addressed event ids.

Every id is derived entirely from immutable facts about the event, so indexing the
same commit twice produces the same id and `merge_insert` updates in place. There is
no counter, no watermark arithmetic, and therefore no watermark bug that can
duplicate a row.

This is the one place the design is strictly better than the transcript indexer it
sits beside: that one's uid names a LINE SLOT in a file rather than an event, so a
resumed session reuses ids for different content (one pair held 1,018 slots whose
contents differed between copies). A git sha cannot do that.

Ids are opaque. Nothing parses them back apart — `:` appears inside paths and that
is fine, because uniqueness is the only property required.
"""

from __future__ import annotations


def commit_id(unit: str, sha: str) -> str:
    return f"git:{unit}:{sha}"


def file_change_id(unit: str, sha: str, path: str) -> str:
    return f"git:{unit}:{sha}:{path}"


def submodule_bump_id(unit: str, sha: str, sub_path: str) -> str:
    return f"git:{unit}:{sha}:gitlink:{sub_path}"


def gh_event_id(repo: str, number: int, event_id: str) -> str:
    """`repo` is owner/name — a submodule's issues live in its own GitHub repo."""
    return f"gh:{repo}:{number}:{event_id}"
