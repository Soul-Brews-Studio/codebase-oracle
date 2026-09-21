"""A submodule's `.git` is a FILE, and mistaking that for "not initialised" is silent.

This regression cost two submodules' entire history on a real repo: `os.path.isdir`
reported both as uninitialised, `discover()` marked them `indexed: false`, and `status`
printed a confident `0 events` for each. Nothing errored.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from codebase_oracle.units import discover


def _git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True)


@pytest.fixture
def superproject(tmp_path):
    """A real superproject with one real submodule, built on disk."""
    inner = tmp_path / "inner"
    inner.mkdir()
    _git(str(inner), "init", "-q", "-b", "main")
    (inner / "a.txt").write_text("one\n")
    _git(str(inner), "add", "a.txt")
    _git(str(inner), "-c", "user.email=t@t", "-c", "user.name=T", "commit", "-qm", "inner one")

    outer = tmp_path / "outer"
    outer.mkdir()
    _git(str(outer), "init", "-q", "-b", "main")
    (outer / "r.txt").write_text("root\n")
    _git(str(outer), "add", "r.txt")
    _git(str(outer), "-c", "user.email=t@t", "-c", "user.name=T", "commit", "-qm", "root one")
    subprocess.run(
        ["git", "-C", str(outer), "-c", "protocol.file.allow=always",
         "submodule", "add", "-q", str(inner), "vendor/inner"],
        check=True, capture_output=True,
    )
    _git(str(outer), "-c", "user.email=t@t", "-c", "user.name=T", "commit", "-qm", "add submodule")
    return str(outer)


def test_submodule_git_is_a_file_not_a_directory(superproject):
    """The premise of the bug. If this ever fails, git changed and the guard can relax."""
    dotgit = os.path.join(superproject, "vendor", "inner", ".git")
    assert os.path.exists(dotgit)
    assert not os.path.isdir(dotgit), "isdir() would have been a safe check after all"


def test_initialised_submodule_is_reported_as_indexable(superproject):
    units = {u.unit: u for u in discover(superproject)}
    assert "vendor/inner" in units
    assert units["vendor/inner"].initialised is True


def test_deinitialised_submodule_degrades_honestly(superproject):
    subprocess.run(
        ["git", "-C", superproject, "submodule", "deinit", "-f", "vendor/inner"],
        check=True, capture_output=True,
    )
    units = {u.unit: u for u in discover(superproject)}
    # Still discovered — the superproject's gitlinks are readable either way, so the
    # bumps survive. Only its own commits are out of reach.
    assert "vendor/inner" in units
    assert units["vendor/inner"].initialised is False
