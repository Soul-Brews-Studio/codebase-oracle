import os

from codebase_oracle.units import codebase_root, discover

HERE = os.path.dirname(os.path.abspath(__file__))


def test_root_is_found_from_a_subdirectory():
    root = codebase_root(HERE)
    assert os.path.isdir(os.path.join(root, ".git")) or os.path.exists(
        os.path.join(root, ".git")
    )


def test_superproject_is_always_the_first_unit():
    units = discover(codebase_root(HERE))
    assert units[0].unit == "."
    assert units[0].initialised


def test_unit_names_are_unique():
    units = discover(codebase_root(HERE))
    names = [u.unit for u in units]
    assert len(names) == len(set(names))
