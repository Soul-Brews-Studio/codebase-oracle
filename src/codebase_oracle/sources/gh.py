"""GitHub issue and PR transitions, via the timeline API.

`gh issue view 43` reports that #43 is OPEN. It cannot tell you when it was closed and
reopened, who labelled it, or what the discussion was — GitHub's issue object holds
current STATE, and this project's whole premise is that state is a fold over
transitions. So the source is `/issues/{n}/timeline`, and the transitions are persisted
locally because GitHub will happily mutate the issue underneath us.

`/issues` covers pull requests too; a PR is an issue carrying a `pull_request` key.
That is also why the timeline endpoint works for both.

No ETag cache, deliberately. `since=<watermark>` already excludes every unchanged issue
from the listing, which is where the pagination cost lives. An ETag would only help on
per-issue timeline fetches for issues that DID change, and those must be re-read by
definition.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator

from ..ids import gh_event_id
from ..models import EventRow
from ..time import to_utc_iso

# Events that say something happened. The timeline also carries cross-references,
# mentions, and subscription noise, which describe nobody's decision.
#
# `committed` is deliberately EXCLUDED. A PR timeline replays every commit on the
# branch, which the git source already holds — keeping them double-counted the same
# work (169 pr-transitions for ~40 PRs) and carried no actor, because a `committed`
# entry has author/committer objects rather than the `actor` every other event uses.
# The PR-to-commit link is recoverable from the merge commit; the duplicate text is not
# worth it.
_KEEP = {
    "closed",
    "reopened",
    "labeled",
    "unlabeled",
    "commented",
    "merged",
    "referenced",
    "assigned",
    "milestoned",
    "renamed",
    "reviewed",
    "review_requested",
    "head_ref_force_pushed",
}


class GhUnavailable(RuntimeError):
    pass


def available() -> bool:
    return shutil.which("gh") is not None


def _api_lines(path: str) -> list[dict]:
    """`gh api --paginate --jq '.[]'` yields one JSON object per line.

    Without `--jq`, `--paginate` concatenates whole JSON arrays back to back, which is
    not parseable as a single document.
    """
    p = subprocess.run(
        ["gh", "api", "--paginate", "--jq", ".[]", path],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if p.returncode != 0:
        raise GhUnavailable(p.stderr.strip() or f"gh api {path} failed")
    out = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def read(unit: str, repo: str, since: str = "") -> Iterator[EventRow]:
    """Transitions for every issue and PR in `repo` touched since `since`."""
    if not available():
        raise GhUnavailable("gh is not installed")

    q = "state=all&per_page=100&sort=updated&direction=asc"
    if since:
        q += f"&since={since}"
    issues = _api_lines(f"/repos/{repo}/issues?{q}")

    for issue in issues:
        number = issue.get("number")
        if not number:
            continue
        is_pr = "pull_request" in issue
        kind = "pr-transition" if is_pr else "issue-transition"
        title = issue.get("title") or ""

        # The timeline does NOT include the issue being opened, so it comes from the
        # issue object. Without this the log would start mid-conversation.
        opened_at = to_utc_iso(issue.get("created_at"))
        yield _row(
            uid=gh_event_id(repo, number, "opened"),
            unit=unit,
            kind=kind,
            number=number,
            ts=opened_at,
            actor=(issue.get("user") or {}).get("login", ""),
            text=f"opened: {title}\n\n{issue.get('body') or ''}".strip(),
        )

        for ev in _api_lines(f"/repos/{repo}/issues/{number}/timeline?per_page=100"):
            name = ev.get("event") or ""
            if name not in _KEEP:
                continue
            ev_id = str(ev.get("id") or ev.get("node_id") or f"{name}:{ev.get('created_at')}")
            actor = (ev.get("actor") or ev.get("user") or {}).get("login", "")
            yield _row(
                uid=gh_event_id(repo, number, ev_id),
                unit=unit,
                kind=kind,
                number=number,
                ts=to_utc_iso(ev.get("created_at") or ev.get("submitted_at")),
                actor=actor,
                text=_text(name, title, ev),
                sha=ev.get("commit_id") or "",
            )


def _text(name: str, title: str, ev: dict) -> str:
    body = (ev.get("body") or "").strip()
    if name == "commented" and body:
        return body
    if name == "labeled" or name == "unlabeled":
        return f"{name}: {(ev.get('label') or {}).get('name', '')} — {title}"
    if body:
        return f"{name}: {title}\n\n{body}"
    return f"{name}: {title}"


def _row(
    *,
    uid: str,
    unit: str,
    kind: str,
    number: int,
    ts: str,
    actor: str,
    text: str,
    sha: str = "",
) -> EventRow:
    # A GitHub event has ONE timestamp, so author-time and record-time genuinely
    # coincide. Both columns are filled so that ordering and range filters work
    # identically across git and gh events.
    return EventRow(
        uid=uid,
        unit=unit,
        kind=kind,
        ts_author=ts,
        ts_commit=ts,
        sha=sha,
        path="",
        text=text,
        author_human=actor,
        author_agent="",
        author_model="",
        refs=f"#{number}",
        number=number,
        insertions=0,
        deletions=0,
        from_sha="",
        to_sha="",
    )
