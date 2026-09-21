"""Traversal over real git histories built on disk.

The fixtures shell out to git rather than writing rows by hand, because the bugs this
guards against live in the join between what git prints and what we store. A
hand-written diamond would have passed every one of them.

The headline case is the diamond merge. The equivalent DuckDB recursive CTE returned
**24,535,176** ancestors for an 11,293-commit history, because `UNION` inside
`WITH RECURSIVE` dedupes on the whole row and every distinct path through a merge is a
fresh row. Dedupe must be on the NODE, and `test_diamond_counts_each_ancestor_once`
pins that.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

lancedb = pytest.importorskip("lancedb")

# E402 is correct in general and wrong here: these imports pull in lancedb
# transitively, so hoisting them above importorskip turns a clean skip on a machine
# without lancedb into a collection error.
from codebase_oracle.graph import (  # noqa: E402
    Adjacency,
    Edge,
    commit_node,
    contradictions,
    extern_node,
    issue_node,
    iter_edges,
)
from codebase_oracle.indexer import run_index, store_dir  # noqa: E402
from codebase_oracle.store import Store  # noqa: E402

REPO = "acme/widget"


def _git(cwd: Path, *args: str, date: str = "") -> str:
    """Run git, optionally pinning BOTH timestamps.

    The committer date has no config key — `-c committer.date=…` is silently ignored and
    the commit gets `now`. It is settable only through `GIT_COMMITTER_DATE`, so both
    dates go through the environment and neither can drift from the other.
    """
    env = dict(os.environ)
    if date:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    p = subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=T", *args],
        check=True, capture_output=True, text=True, env=env,
    )
    return p.stdout.strip()


def _commit(cwd: Path, message: str, date: str) -> str:
    """Commit at a fixed instant.

    Without pinning, every commit in a fixture lands in the same second, `ts_commit`
    ties, and the `--until` fold has nothing to cut on — the test would then pass by
    accident on a slow machine and fail on a fast one.
    """
    _git(cwd, "commit", "-q", "--allow-empty", "-m", message, date=date)
    return _git(cwd, "rev-parse", "HEAD")


def _init(cwd: Path) -> None:
    cwd.mkdir(parents=True, exist_ok=True)
    _git(cwd, "init", "-q", "-b", "main")
    # A GitHub remote, because issue edges are only emitted for units with one — without
    # it `closes #44` produces no edge and the contradiction tests silently pass empty.
    _git(cwd, "remote", "add", "origin", f"https://github.com/{REPO}")


def _index(root: Path) -> Store:
    run_index(str(root), with_gh=False, progress=lambda _m: None)
    return Store(store_dir(str(root)))


# --------------------------------------------------------------------- fixtures

@pytest.fixture
def diamond(tmp_path: Path) -> Path:
    """A -> B -> D, A -> C -> D. D is a real merge with two parents.

        A   2026-01-01
        |\\
        B C 2026-01-02 / 2026-01-03
        |/
        D   2026-01-04  (merge)
    """
    root = tmp_path / "diamond"
    _init(root)
    (root / "a.txt").write_text("a\n")
    _git(root, "add", "a.txt")
    _commit(root, "A root", "2026-01-01T00:00:00+00:00")

    _git(root, "branch", "side")
    (root / "b.txt").write_text("b\n")
    _git(root, "add", "b.txt")
    _commit(root, "B on main", "2026-01-02T00:00:00+00:00")

    _git(root, "checkout", "-q", "side")
    (root / "c.txt").write_text("c\n")
    _git(root, "add", "c.txt")
    _commit(root, "C on side", "2026-01-03T00:00:00+00:00")

    _git(root, "checkout", "-q", "main")
    _git(root, "merge", "-q", "--no-ff", "--no-edit", "side",
         date="2026-01-04T00:00:00+00:00")
    return root


@pytest.fixture
def superproject(tmp_path: Path) -> Path:
    """A superproject whose commit bumps a submodule that has its own history."""
    inner = tmp_path / "inner"
    _init(inner)
    (inner / "i.txt").write_text("one\n")
    _git(inner, "add", "i.txt")
    _commit(inner, "inner one", "2026-02-01T00:00:00+00:00")

    outer = tmp_path / "outer"
    _init(outer)
    (outer / "r.txt").write_text("root\n")
    _git(outer, "add", "r.txt")
    _commit(outer, "root one", "2026-02-02T00:00:00+00:00")
    _git(outer, "-c", "protocol.file.allow=always",
         "submodule", "add", "-q", str(inner), "vendor/inner")
    _commit(outer, "bump vendor/inner", "2026-02-03T00:00:00+00:00")
    return outer


# ------------------------------------------------------------------ the diamond

def test_diamond_counts_each_ancestor_once(diamond: Path) -> None:
    """The 24-million-row bug, in miniature.

    A is reachable from D by two distinct paths (via B and via C). Path-based dedupe
    counts it twice; node-based dedupe counts it once and keeps the SHORTER distance.
    """
    store = _index(diamond)
    adj = Adjacency.load(store)
    head = _git(diamond, "rev-parse", "HEAD")

    anc = adj.ancestors(commit_node(".", head))
    assert len(anc) == 4, f"expected 4 distinct ancestors, got {len(anc)}: {sorted(anc)}"

    a_sha = _git(diamond, "rev-list", "--max-parents=0", "HEAD")
    assert anc[commit_node(".", head)] == 0
    assert anc[commit_node(".", a_sha)] == 2, "the shared root must keep its shortest depth"


def test_ancestry_matches_git_rev_list(diamond: Path) -> None:
    """Ground truth, not self-consistency. `rev-list --count` also counts the tip."""
    store = _index(diamond)
    adj = Adjacency.load(store)
    head = _git(diamond, "rev-parse", "HEAD")
    expected = int(_git(diamond, "rev-list", "--count", "HEAD"))
    assert len(adj.ancestors(commit_node(".", head))) == expected


def test_merge_commit_keeps_both_parents(diamond: Path) -> None:
    """`parents` is space-joined; splitting it wrong loses half the DAG silently."""
    store = _index(diamond)
    adj = Adjacency.load(store)
    head = _git(diamond, "rev-parse", "HEAD")
    out = [e for e in adj.fwd[commit_node(".", head)] if e.kind == "wasDerivedFrom"]
    assert len(out) == 2


def test_descendants_invert_ancestors(diamond: Path) -> None:
    store = _index(diamond)
    adj = Adjacency.load(store)
    a_sha = _git(diamond, "rev-list", "--max-parents=0", "HEAD")
    head = _git(diamond, "rev-parse", "HEAD")
    assert commit_node(".", head) in adj.descendants(commit_node(".", a_sha))


def test_between_is_the_log_range(diamond: Path) -> None:
    """`between(A, HEAD)` must equal `git log A..HEAD`."""
    store = _index(diamond)
    adj = Adjacency.load(store)
    a_sha = _git(diamond, "rev-list", "--max-parents=0", "HEAD")
    head = _git(diamond, "rev-parse", "HEAD")
    got = adj.between(commit_node(".", a_sha), commit_node(".", head))
    expected = int(_git(diamond, "rev-list", "--count", f"{a_sha}..HEAD"))
    assert len(got) == expected


# ----------------------------------------------------------------- the fold

def test_until_strictly_folds(diamond: Path) -> None:
    """A graph that can only answer for HEAD is just another snapshot.

    Folding to the root commit's own timestamp must yield strictly fewer edges, and no
    edge may carry a `ts` after the cut.
    """
    store = _index(diamond)
    cut = "2026-01-01T00:00:00+00:00"

    full = list(iter_edges(store))
    folded = list(iter_edges(store, until=cut))

    assert 0 < len(folded) < len(full)
    assert all(e.ts <= cut for e in folded)


def test_until_shrinks_ancestry(diamond: Path) -> None:
    store = _index(diamond)
    head = _git(diamond, "rev-parse", "HEAD")
    cut = "2026-01-02T00:00:00+00:00"
    # The merge does not exist yet at the cut, so its node has no edges at all and the
    # walk returns the start alone rather than crashing on a missing key.
    adj = Adjacency.load(store, until=cut)
    assert len(adj.ancestors(commit_node(".", head))) == 1


# -------------------------------------------------------------- dangling nodes

def test_dangling_dst_is_kept_not_dropped() -> None:
    """A `referenced` sha can belong to another repository entirely.

    Dropping an endpoint with no event row would shrink the graph silently. It is kept
    and spelled `extern:` so the reader can tell "someone else's" from "not read yet".
    """
    far = extern_node("other/repo", "deadbeef" * 5)
    here = commit_node(".", "a" * 40)
    adj = Adjacency([Edge(here, far, "mentions", "gh-referenced", "2026-01-01T00:00:00+00:00")])

    assert far in adj.nodes
    assert adj.walk(here) == {here: 0, far: 1}
    assert adj.path(here, far) == [here, far]


def test_walk_on_an_unknown_node_returns_just_itself() -> None:
    adj = Adjacency([])
    assert adj.walk("git:.:nope") == {"git:.:nope": 0}


# ------------------------------------------------------------- cross-unit hop

def test_bump_crosses_into_the_submodule_history(superproject: Path) -> None:
    """The query `git rev-list` cannot express — it stops dead at a gitlink.

    The bump's `path` IS the submodule's unit name, so the edge lands on a node that the
    submodule's own indexed history also produced. That shared node is the hop.
    """
    store = _index(superproject)
    adj = Adjacency.load(store)

    inner_head = _git(superproject / "vendor" / "inner", "rev-parse", "HEAD")
    target = commit_node("vendor/inner", inner_head)

    bumps = adj.edges_of_kind("bumped")
    assert [e for e in bumps if e.dst == target], f"no bump reached {target}"

    # And the node is real on the other side: the submodule's own commit row produced it.
    assert target in adj.nodes
    outer_head = _git(superproject, "rev-parse", "HEAD")
    assert adj.path(commit_node(".", outer_head), target), "no path across the gitlink"


def test_units_are_separate_histories(superproject: Path) -> None:
    """Superproject ancestry must not absorb the submodule's commits."""
    store = _index(superproject)
    adj = Adjacency.load(store)
    outer_head = _git(superproject, "rev-parse", "HEAD")
    anc = adj.ancestors(commit_node(".", outer_head))
    expected = int(_git(superproject, "rev-list", "--count", "HEAD"))
    assert len(anc) == expected
    assert all(n.startswith("git:.:") for n in anc)


# ------------------------------------------------- claims versus what happened

def test_closes_and_mentions_are_different_edges(tmp_path: Path) -> None:
    """A 0.60-precision `#N` scrape must never masquerade as a closure claim."""
    root = tmp_path / "claims"
    _init(root)
    (root / "f.txt").write_text("x\n")
    _git(root, "add", "f.txt")
    _commit(root, "fix(x): closes #44, see also #45", "2026-03-01T00:00:00+00:00")

    store = _index(root)
    adj = Adjacency.load(store)

    closes = {e.dst: e for e in adj.edges_of_kind("closes")}
    mentions = {e.dst: e for e in adj.edges_of_kind("mentions")}

    assert set(closes) == {issue_node(REPO, 44)}
    assert closes[issue_node(REPO, 44)].source == "regex-keyword"
    assert set(mentions) == {issue_node(REPO, 45)}
    assert mentions[issue_node(REPO, 45)].source == "regex-hash"
    # #44 was claimed, so it must not ALSO appear as a bare mention.
    assert issue_node(REPO, 44) not in mentions


def test_regex_keyword_is_not_trusted() -> None:
    ts = "2026-01-01T00:00:00+00:00"
    assert not Edge("a", "b", "closes", "regex-keyword", ts).trusted
    assert not Edge("a", "b", "mentions", "regex-hash", ts).trusted
    # GitHub resolved this one itself — discarding it under --trusted would throw away a
    # reliable link as if it were our own scraping.
    assert Edge("a", "b", "mentions", "gh-referenced", ts).trusted
    assert Edge("a", "b", "wasDerivedFrom", "parent", ts).trusted


def test_missing_gh_data_is_not_a_contradiction(tmp_path: Path) -> None:
    """Only 42.2% of issue-commit links are recoverable, so silence is missing data.

    Reporting an unindexed issue as a broken promise would manufacture findings out of
    the gaps in our own coverage.
    """
    root = tmp_path / "unverified"
    _init(root)
    (root / "f.txt").write_text("x\n")
    _git(root, "add", "f.txt")
    _commit(root, "fix(x): closes #44", "2026-03-01T00:00:00+00:00")

    store = _index(root)  # with_gh=False, so no transitions exist
    found = contradictions(store, Adjacency.load(store))

    assert len(found) == 1
    assert found[0].issue == issue_node(REPO, 44)
    assert found[0].verdict == "gh-not-indexed"


def test_a_bare_mention_never_accuses(tmp_path: Path) -> None:
    root = tmp_path / "mention-only"
    _init(root)
    (root / "f.txt").write_text("x\n")
    _git(root, "add", "f.txt")
    _commit(root, "chore: touches #44 somehow", "2026-03-01T00:00:00+00:00")

    store = _index(root)
    assert contradictions(store, Adjacency.load(store)) == []
