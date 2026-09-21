"""ONE clock. Storage is UTC, display is local, and nothing else converts.

Two prior fleet bugs live behind this module. A reference implementation sliced a
stored ISO string in one command and converted it in another, so the same session
reported both 10:06 and 17:06. Separately, a beat table stored Bangkok local time
labelled `Z`, so anyone "correcting" for the offset moved it seven hours the wrong
way.

git makes it worse by having TWO dates per commit — author date and commit date —
which rebase and cherry-pick leave deliberately different. Both are stored; neither
is derived from the other.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Union

Stamp = Union[str, int, float, None]


def _dt(v: Stamp) -> datetime | None:
    """Parse anything we might have stored into an aware datetime, or None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        # Milliseconds and seconds are indistinguishable without a magnitude test.
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def to_utc_iso(v: Stamp) -> str:
    """The ONLY storage format: UTC, offset written as +00:00.

    Normalising to a single offset is what makes the column lexically sortable.
    git's own `%aI` carries the committer's local offset, so two commits one second
    apart can sort backwards as raw strings.
    """
    d = _dt(v)
    if d is None:
        return ""
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def local_date_time(v: Stamp) -> str:
    d = _dt(v)
    return d.astimezone().strftime("%Y-%m-%d %H:%M") if d else ""


def local_date(v: Stamp) -> str:
    d = _dt(v)
    return d.astimezone().strftime("%Y-%m-%d") if d else ""


def local_time(v: Stamp) -> str:
    d = _dt(v)
    return d.astimezone().strftime("%H:%M") if d else ""


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def zone_offset() -> str:
    off = datetime.now().astimezone().utcoffset()
    if off is None:
        return "+00:00"
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    h, m = total // 3600, (total % 3600) // 60
    return f"{sign}{h:02d}:{m:02d}" if m else f"{sign}{h:02d}"
