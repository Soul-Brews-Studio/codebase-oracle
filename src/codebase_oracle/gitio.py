"""Running git. Argument lists only — never a shell string.

Every call here is read-only. This tool observes a repository; it must never be able
to modify one, so no command in this codebase writes to git.
"""

from __future__ import annotations

import subprocess


class GitError(RuntimeError):
    pass


def git(cwd: str, *args: str, check: bool = True) -> str:
    """Run git in `cwd` and return stdout stripped of its trailing newline."""
    p = subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if p.returncode != 0:
        if check:
            raise GitError(f"git {' '.join(args)} failed in {cwd}: {p.stderr.strip()}")
        return ""
    return p.stdout.rstrip("\n")


def git_lines(cwd: str, *args: str, check: bool = True) -> list[str]:
    out = git(cwd, *args, check=check)
    return out.split("\n") if out else []


def git_z(cwd: str, *args: str, check: bool = True) -> list[str]:
    """For `-z` output. Paths can contain newlines; NUL-separated output cannot lie."""
    out = git(cwd, *args, check=check)
    return [s for s in out.split("\0") if s]


def git_ok(cwd: str, *args: str) -> bool:
    """True when git exits 0. For commands that answer through their exit status.

    `merge-base --is-ancestor` prints nothing either way, so a stdout-based helper
    cannot distinguish yes from no — it reads both as the empty string.
    """
    p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    return p.returncode == 0
