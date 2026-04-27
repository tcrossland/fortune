"""Tree-declared classification tests.

Every fixture under ``tests/fixtures/`` lives at a path of the form
``<language>/<bank>/<doctype>/<filename>``. The folder names are the exact
enum values for the expected classification, so the path itself is the
ground truth. This module turns each discovered fixture into an individual
pytest case that asserts the layered classifier agrees with the path.

Drop a new file into the tree and coverage follows automatically.
"""

from __future__ import annotations

import pytest

from banking_pipeline.classifiers.hybrid import LayeredClassifier
from tests.conftest import FixtureCase, discover_fixtures


_CASES = discover_fixtures()


@pytest.mark.skipif(not _CASES, reason="No fixtures discovered under tests/fixtures/")
@pytest.mark.parametrize(
    "case",
    _CASES,
    ids=[str(c.relative_path) for c in _CASES],
)
def test_fixture_matches_tree_declared_classification(case: FixtureCase) -> None:
    result = LayeredClassifier().classify(case.load())

    # Language
    assert result.language is not None
    assert result.language.language is case.language, (
        f"{case.relative_path}: expected language={case.language}, "
        f"got {result.language.language}"
    )

    # Bank (only asserted when the declaring folder is a non-UNKNOWN bank)
    assert result.bank is not None
    assert result.bank.bank is case.bank, (
        f"{case.relative_path}: expected bank={case.bank}, got {result.bank.bank}"
    )

    # Document type
    assert result.document_type is case.doctype, (
        f"{case.relative_path}: expected doctype={case.doctype}, "
        f"got {result.document_type}"
    )


def test_every_txt_in_fixture_tree_is_classifiable() -> None:
    """Loud failure if someone adds a fixture at the wrong depth or with a
    misspelled folder name — discover_fixtures silently skips such files, and
    this test catches it."""

    from tests.conftest import FIXTURES_DIR

    all_txt = sorted(FIXTURES_DIR.rglob("*.txt"))
    discovered_paths = {c.path for c in discover_fixtures()}
    unclassifiable = [p for p in all_txt if p not in discovered_paths]

    assert not unclassifiable, (
        "These .txt files aren't at <lang>/<bank>/<doctype>/<file> depth or "
        f"their folder names don't map to enum values: {unclassifiable}"
    )
