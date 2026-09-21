"""Typed directed edges over the event log, and traversal of them.

There is no edges table, deliberately. Every edge below is already a projection of a
column in `events`: parents on the commit row, `refs` on the commit row, the sha on a
file-change row, the gitlink pair on a bump row, the commit_id on a GitHub transition.
Materialising them would store no new information — and in one measured knowledge graph
provenance edges grew to 70% of all edges for exactly that reason. A table becomes
justified the day an edge exists that is NOT a projection; the session join is the first.

`iter_edges` is therefore the single edge producer, and the only contract callers depend
on. If storage ever changes, it slots in behind this function and nothing else moves.

Traversal is in memory because the numbers say so. A full scan of 50,996 rows takes
0.01s, while LanceDB exposes no local SQL at all (`db.execute_query()` raises
NotImplementedError — cloud only), so there is nothing to push down. Loading the whole
edge set and walking a dict measured 96.2ms over 11,293 commits against 16s at 623% CPU
for the equivalent DuckDB recursive CTE.

That CTE also returned 24,535,176 ancestors where the answer was 11,293, because `UNION`
inside `WITH RECURSIVE` dedupes on the whole row, so every distinct path through a merge
commit is a fresh row. The lesson is not about DuckDB: **any** traversal must dedupe on
the node, never on the path. The `visited` set below is that rule.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from .models import TRUSTED_SOURCES
from .store import Row, Store

_REFS = re.compile(r"#(\d+)")

# GitHub's own closing keywords. A commit carrying one is making an explicit CLAIM about
# an issue, which is a different thing from mentioning it — and the claim is checkable
# against whether the issue ever actually closed.
_CLOSES = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[:\s]+#(\d+)",
    re.IGNORECASE,
)


# ------------------------------------------------------------------ node ids

def commit_node(unit: str, sha: str) -> str:
    return f"git:{unit}:{sha}"


def file_node(unit: str, path: str) -> str:
    return f"file:{unit}:{path}"


def issue_node(repo: str, number: int | str) -> str:
    return f"issue:{repo}:{number}"


def extern_node(repo: str, sha: str) -> str:
    """A commit in another repository.

    Kept distinct from `commit_node` so a reader can see that the far end is outside this
    codebase rather than merely unindexed. Collapsing the two would turn "belongs to
    someone else" into "we have not read it yet", which are different answers.
    """
    return f"extern:{repo}:{sha}"


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    kind: str
    source: str
    ts: str

    @property
    def trusted(self) -> bool:
        return self.source in TRUSTED_SOURCES


# ------------------------------------------------------------------ producer

def iter_edges(store: Store, until: str = "", unit: str = "") -> Iterator[Edge]:
    """Every edge the log implies, optionally folded to a point in time.

    `until` is first-class rather than an afterthought: the premise of this tool is that
    state is a fold over events, and a graph that can only answer for HEAD is the same
    snapshot every other code graph in the fleet already offers.
    """
    urls = {str(u["unit"]): str(u["url"]) for u in store.units()}

    for r in store.range(until=until, unit=unit, limit=0):
        kind = str(r.get("kind") or "")
        if kind == "commit":
            yield from _commit_edges(r, urls)
        elif kind == "file-change":
            yield _touched(r)
        elif kind == "submodule-bump":
            edge = _bumped(r)
            if edge:
                yield edge
        elif kind in ("issue-transition", "pr-transition"):
            edge = _gh_edge(r, urls)
            if edge:
                yield edge


def _commit_edges(r: Row, urls: dict[str, str]) -> Iterator[Edge]:
    unit, sha, ts = str(r["unit"]), str(r["sha"]), str(r["ts_commit"])
    src = commit_node(unit, sha)

    # The DAG. A merge commit has several parents, which is exactly the shape that makes
    # path-based dedupe explode.
    for parent in str(r.get("parents") or "").split():
        yield Edge(src, commit_node(unit, parent), "wasDerivedFrom", "parent", ts)

    # Two different things get scraped out of the message, and conflating them is how a
    # 0.60-precision regex turns into a false accusation.
    #
    #   "closes #44"  -> a CLAIM by the author. Checkable: did #44 ever close?
    #   "#44"         -> a mention. Issue-to-commit recovery by heuristic finds at most
    #                    half the real links at ~0.60 precision, so it proves nothing.
    repo = urls.get(unit, "")
    if repo:
        text = str(r.get("text") or "")
        claimed = dict.fromkeys(_CLOSES.findall(text))
        for n in claimed:
            yield Edge(src, issue_node(repo, n), "closes", "regex-keyword", ts)
        for n in dict.fromkeys(_REFS.findall(text)):
            if n not in claimed:
                yield Edge(src, issue_node(repo, n), "mentions", "regex-hash", ts)

    agent = str(r.get("author_agent") or "")
    if agent:
        model = str(r.get("author_model") or "")
        who = f"{agent} {model}".strip()
        yield Edge(src, f"agent:{who}", "wasAttributedTo", "trailer", ts)


def _touched(r: Row) -> Edge:
    unit, sha = str(r["unit"]), str(r["sha"])
    return Edge(
        commit_node(unit, sha),
        file_node(unit, str(r["path"])),
        "touched",
        "file-change",
        str(r["ts_commit"]),
    )


def _bumped(r: Row) -> Edge | None:
    """The cross-unit hop, and the reason `units` exists.

    A superproject commit moves a gitlink; `path` is the submodule's path, which IS that
    submodule's unit name, and `to_sha` is a commit in the submodule's own history. So
    this single edge crosses from one git history into another — something `git rev-list`
    cannot express, because it stops dead at a gitlink.
    """
    to_sha = str(r.get("to_sha") or "")
    if not to_sha or set(to_sha) == {"0"}:  # all-zero = submodule removed
        return None
    return Edge(
        commit_node(str(r["unit"]), str(r["sha"])),
        commit_node(str(r["path"]), to_sha),
        "bumped",
        "gitlink",
        str(r["ts_commit"]),
    )


def _gh_edge(r: Row, urls: dict[str, str]) -> Edge | None:
    """A GitHub transition that names a commit.

    `closed` with a commit_id is GitHub resolving a closing keyword, and `merged` is
    GitHub recording the merge — both far better than our own regex. `referenced` is only
    a mention, and its sha frequently belongs to another repository entirely.
    """
    sha = str(r.get("sha") or "")
    if not sha:
        return None
    event = str(r.get("gh_event") or "")
    unit = str(r["unit"])
    repo = urls.get(unit, "")
    if not repo:
        return None

    sha_repo = str(r.get("sha_repo") or "")
    commit = extern_node(sha_repo, sha) if sha_repo else commit_node(unit, sha)
    issue = issue_node(repo, int(r.get("number") or 0))
    ts = str(r["ts_commit"])

    if event == "closed":
        return Edge(commit, issue, "resolves", "gh-closed", ts)
    if event == "merged":
        return Edge(issue, commit, "merged_as", "gh-merged", ts)
    if event == "referenced":
        # GitHub resolved this cross-reference itself, so the LINK is reliable even though
        # it is not evidence of closure. Labelling it regex-hash would have made
        # `--trusted` discard a GitHub-provided fact as if it were our own scraping.
        return Edge(commit, issue, "mentions", "gh-referenced", ts)
    return None


# ----------------------------------------------------------------- traversal

class Adjacency:
    """Forward and reverse adjacency over a loaded edge set.

    `nodes` holds every endpoint, including ones with no event row of their own — a
    referenced sha from another repository, or a submodule that was never initialised.
    Dropping those would silently shrink the graph; they are kept and rendered as
    external so the gap is visible.
    """

    def __init__(self, edges: Iterable[Edge]) -> None:
        self.fwd: dict[str, list[Edge]] = defaultdict(list)
        self.rev: dict[str, list[Edge]] = defaultdict(list)
        self.nodes: set[str] = set()
        self.count = 0
        for e in edges:
            self.fwd[e.src].append(e)
            self.rev[e.dst].append(e)
            self.nodes.add(e.src)
            self.nodes.add(e.dst)
            self.count += 1

    @classmethod
    def load(cls, store: Store, until: str = "", unit: str = "") -> Adjacency:
        return cls(iter_edges(store, until=until, unit=unit))

    def walk(
        self,
        start: str,
        *,
        kinds: tuple[str, ...] = (),
        depth: int | None = None,
        reverse: bool = False,
        trusted_only: bool = False,
    ) -> dict[str, int]:
        """Reachable nodes mapped to their distance, INCLUDING `start` at 0.

        Dedupe is on the node. A diamond merge reaches the same ancestor by two paths and
        it must be counted once — the failure that produced 24,535,176 rows for 11,293
        commits came from deduping on the path instead.
        """
        side = self.rev if reverse else self.fwd
        seen: dict[str, int] = {start: 0}
        q: deque[str] = deque([start])
        while q:
            node = q.popleft()
            d = seen[node]
            if depth is not None and d >= depth:
                continue
            for e in side.get(node, ()):
                if kinds and e.kind not in kinds:
                    continue
                if trusted_only and not e.trusted:
                    continue
                nxt = e.src if reverse else e.dst
                if nxt not in seen:
                    seen[nxt] = d + 1
                    q.append(nxt)
        return seen

    def ancestors(self, node: str, until_depth: int | None = None) -> dict[str, int]:
        """Commits reachable by following parent links, including `node` itself.

        Including the start is what makes this directly comparable to
        `git rev-list --count HEAD`, which also counts the tip.
        """
        return self.walk(node, kinds=("wasDerivedFrom",), depth=until_depth)

    def descendants(self, node: str) -> dict[str, int]:
        return self.walk(node, kinds=("wasDerivedFrom",), reverse=True)

    def between(self, older: str, newer: str) -> dict[str, int]:
        """Commits in `newer`'s ancestry but not `older`'s — `git log older..newer`."""
        return {
            n: d
            for n, d in self.ancestors(newer).items()
            if n not in self.ancestors(older)
        }

    def neighbours(self, node: str) -> list[Edge]:
        out = list(self.fwd.get(node, ()))
        out.extend(self.rev.get(node, ()))
        return out

    def edges_of_kind(self, kind: str) -> list[Edge]:
        return [e for edges in self.fwd.values() for e in edges if e.kind == kind]

    def path(self, a: str, b: str) -> list[str]:
        """Shortest path a→b over all edge kinds, or [] when unreachable."""
        prev: dict[str, str] = {}
        seen = {a}
        q: deque[str] = deque([a])
        while q:
            node = q.popleft()
            if node == b:
                out = [b]
                while out[-1] != a:
                    out.append(prev[out[-1]])
                return list(reversed(out))
            for e in self.fwd.get(node, ()):
                if e.dst not in seen:
                    seen.add(e.dst)
                    prev[e.dst] = node
                    q.append(e.dst)
        return []


# ------------------------------------------------------------ contradictions

@dataclass(frozen=True)
class Contradiction:
    """A commit claimed to close an issue, and the issue did not close."""

    commit: str
    issue: str
    claimed_at: str
    subject: str
    verdict: str  # "still-open" | "gh-not-indexed"


def issue_states(store: Store, until: str = "") -> dict[str, str]:
    """Last known state per issue node, folded from transitions in order.

    This is the fold the whole tool is premised on: `open`/`closed` is not stored
    anywhere, it is replayed from `closed` and `reopened` events up to a point in time.
    An issue absent from this map has no indexed transitions at all.
    """
    urls = {str(u["unit"]): str(u["url"]) for u in store.units()}
    state: dict[str, str] = {}
    for r in store.range(until=until, kinds=("issue-transition", "pr-transition"), limit=0):
        repo = urls.get(str(r["unit"]), "")
        if not repo:
            continue
        node = issue_node(repo, int(r.get("number") or 0))
        ev = str(r.get("gh_event") or "")
        if ev == "opened":
            state.setdefault(node, "open")
        elif ev in ("closed", "merged"):
            state[node] = "closed"
        elif ev == "reopened":
            state[node] = "open"
    return state


def contradictions(store: Store, adj: Adjacency, until: str = "") -> list[Contradiction]:
    """Commits whose closing claim GitHub never honoured.

    Fires only on `closes` (an explicit keyword the author wrote), never on `mentions` —
    a bare "#N" at ~0.60 precision would produce a false accusation roughly 40% of the
    time.

    When an issue has no indexed transitions the verdict is `gh-not-indexed`, NOT a
    contradiction. Only 42.2% of issue-commit links are recoverable in the first place, so
    silence here is missing data and must never be reported as a finding.
    """
    states = issue_states(store, until=until)
    subjects = {
        f"git:{r['unit']}:{r['sha']}": str(r.get("text") or "").strip().split("\n")[0]
        for r in store.range(until=until, kinds=("commit",), limit=0)
    }
    out: list[Contradiction] = []
    for e in adj.edges_of_kind("closes"):
        state = states.get(e.dst)
        if state == "closed":
            continue
        out.append(
            Contradiction(
                commit=e.src,
                issue=e.dst,
                claimed_at=e.ts,
                subject=subjects.get(e.src, ""),
                verdict="still-open" if state == "open" else "gh-not-indexed",
            )
        )
    return sorted(out, key=lambda c: c.claimed_at)
