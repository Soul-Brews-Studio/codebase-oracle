from codebase_oracle.sources.git import _is_numstat, _num, parse_authors


def test_plain_human_commit_has_no_agent():
    human, agent, model = parse_authors("Nat", "fix the thing")
    assert (human, agent, model) == ("Nat", "", "")


def test_agent_trailer_is_split_into_vendor_and_model():
    body = "feat: add thing\n\nCo-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
    assert parse_authors("Nat", body) == ("Nat", "Claude", "Sonnet 5")


def test_the_other_trailer_spelling_is_accepted():
    """The fleet writes both 'Co-Authored-By' and 'Co-Author-By'."""
    body = "x\n\nCo-Author-By: Claude Opus 4.6 <noreply@anthropic.com>"
    assert parse_authors("Nat", body) == ("Nat", "Claude", "Opus 4.6")


def test_human_co_author_is_not_recorded_as_an_agent():
    """Guessing from the trailer alone would mislabel every human collaborator."""
    body = "x\n\nCo-Authored-By: Some Person <person@example.com>"
    assert parse_authors("Nat", body) == ("Nat", "", "")


def test_numstat_and_raw_lines_are_distinguishable():
    """The single-pass parser relies on this being unambiguous."""
    assert _is_numstat("12\t3\tsrc/a.py")
    assert _is_numstat("-\t-\tlogo.png")
    assert not _is_numstat(":100644 100644 aaa bbb M\tsrc/a.py")
    assert not _is_numstat("some commit message")


def test_binary_files_count_as_zero_not_a_crash():
    assert _num("-") == 0
    assert _num("12") == 12
