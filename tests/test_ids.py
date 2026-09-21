from codebase_oracle import ids


def test_ids_are_deterministic() -> None:
    a = ids.commit_id(".", "deadbeef")
    b = ids.commit_id(".", "deadbeef")
    assert a == b


def test_kinds_do_not_collide_on_the_same_commit() -> None:
    """A commit and its file changes share a sha and must not share an id."""
    sha = "deadbeef"
    seen = {
        ids.commit_id(".", sha),
        ids.file_change_id(".", sha, "README.md"),
        ids.submodule_bump_id(".", sha, "vendor/lib"),
    }
    assert len(seen) == 3


def test_units_namespace_ids() -> None:
    """The same sha in two units is two events."""
    assert ids.commit_id(".", "abc") != ids.commit_id("vendor/lib", "abc")


def test_file_change_and_bump_on_same_path_differ() -> None:
    """A path can be a file in one commit and a gitlink in another."""
    assert ids.file_change_id(".", "abc", "vendor/lib") != ids.submodule_bump_id(
        ".", "abc", "vendor/lib"
    )


def test_gh_ids_carry_repo_so_submodule_issues_stay_separate() -> None:
    assert ids.gh_event_id("a/b", 1, "x") != ids.gh_event_id("c/d", 1, "x")
