"""What counts as "this codebase", and what histories live inside it.

A codebase is not one history, which is the whole reason this module exists. `git log`
in a superproject contains NO submodule commits — a commit that moves a submodule
pointer records a 40-byte gitlink, not the upstream work. So each git history is a
`unit`: the superproject is `.`, and every submodule is its path.

Units are enumerated from git itself, once, at index time. They are never derived from
the current working directory, which is how the sibling index ended up holding one
repository under three different spellings: macOS folded three differently-cased
directories onto one inode while the index key kept whichever casing the shell
happened to be in. Here there is one codebase per store and its units come from
`git submodule status`, so that bug has nowhere to live.

Vendored libraries and monorepo packages are NOT units. They have no separate history,
so they are simply paths inside the superproject.
"""

from __future__ import annotations

import glob
import os
import re
import subprocess

from .gitio import git, git_lines
from .models import Unit

# `git submodule status` prefixes each line with one character of state:
#   ' ' clean, '-' NOT initialised, '+' checked out at a different sha, 'U' conflicts
_STATUS = re.compile(r"^(?P<flag>[ \-+U])(?P<sha>[0-9a-f]+) (?P<path>.+?)(?: \(.*\))?$")

_GITHUB = re.compile(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)")


def codebase_root(start: str) -> str:
    """The superproject working tree, even when called from inside a submodule.

    `git rev-parse --show-toplevel` answers "which repository am I in", which inside a
    submodule is the submodule — not what we want. Walk up until no superproject
    remains, so running from anywhere in the tree indexes the same codebase.
    """
    cur = os.path.abspath(start)
    top = git(cur, "rev-parse", "--show-toplevel")
    while True:
        parent = git(top, "rev-parse", "--show-superproject-working-tree", check=False)
        if not parent:
            return top
        top = parent


class RepoNotFound(RuntimeError):
    pass


def resolve_repo(name: str) -> str:
    """A codebase path from a bare name, so you can ask about a repo you are not in.

    `ghq root` plus a glob at the known depth, NOT `ghq list`. Measured on this fleet:
    `ghq list --full-path` took 19.9s wall / 89s system at 465% CPU, because it walks
    the whole tree looking for `.git`. The glob reads one directory per org and returns
    in milliseconds. Repo lookup must never cost more than the work it precedes.

    Exact `owner/name` wins, then exact basename, then a unique substring. Ambiguity
    RAISES rather than picking the first hit: a sibling tool resolved `api` to
    `api-gateway` by readdir order, and that near-miss was silent.
    """
    try:
        p = subprocess.run(["ghq", "root"], capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RepoNotFound(f"cannot run ghq: {exc}") from exc
    if p.returncode != 0:
        raise RepoNotFound(p.stderr.strip() or "ghq root failed")

    host = os.path.join(p.stdout.strip(), "github.com")
    want = name.strip("/")

    def repos(pattern: str) -> list[str]:
        return [q for q in glob.glob(os.path.join(host, pattern)) if _is_repo(q)]

    hits = repos(want) if "/" in want else []
    if not hits:
        hits = repos(os.path.join("*", want))
    if not hits:
        hits = repos(os.path.join("*", f"*{want}*"))

    if not hits:
        raise RepoNotFound(f"no repo matching '{name}' under {host}")
    if len(hits) > 1:
        listing = "\n  ".join(sorted(hits))
        raise RepoNotFound(f"'{name}' is ambiguous — name the owner too:\n  {listing}")
    return hits[0]


def _is_repo(path: str) -> bool:
    """A linked worktree's `.git` is a FILE, so test existence rather than isdir."""
    return os.path.exists(os.path.join(path, ".git"))


def github_repo(cwd: str) -> str:
    """`owner/name` from origin, or "" when the remote is missing or not GitHub."""
    url = git(cwd, "remote", "get-url", "origin", check=False)
    m = _GITHUB.search(url) if url else None
    return f"{m['owner']}/{m['repo']}" if m else ""


def discover(root: str) -> list[Unit]:
    """The superproject plus every top-level submodule.

    Nested submodules are out of scope: `--recursive` would report paths relative to
    the superproject and each nested unit would need its own remote resolution, which
    is real work for a case that has not come up yet.
    """
    units = [
        Unit(
            unit=".",
            abs_path=root,
            url=github_repo(root),
            head=git(root, "rev-parse", "HEAD", check=False),
            initialised=True,
        )
    ]

    for line in git_lines(root, "submodule", "status", check=False):
        m = _STATUS.match(line)
        if not m:
            continue
        path = m["path"]
        abs_path = os.path.join(root, path)
        # '-' means no local objects exist. We can still see this submodule's bumps
        # from the superproject's gitlinks, but we cannot read its commits. Recording
        # that honestly is the point: zero events and "cannot be read" are different
        # answers, and only one of them is true.
        # os.path.exists, NOT isdir: inside a submodule `.git` is a FILE containing
        # "gitdir: ../.git/modules/<name>". isdir() therefore reports every correctly
        # initialised submodule as missing, and its entire history is skipped in
        # silence — measured here against two submodules holding 50 and 30 entries.
        initialised = m["flag"] != "-" and os.path.exists(os.path.join(abs_path, ".git"))
        units.append(
            Unit(
                unit=path,
                abs_path=abs_path,
                url=github_repo(abs_path) if initialised else "",
                head=m["sha"],
                initialised=initialised,
            )
        )
    return units
