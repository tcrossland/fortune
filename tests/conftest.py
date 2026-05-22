"""Shared pytest fixtures.

Fixtures on disk live under ``tests/fixtures/`` in a three-level tree:

    tests/fixtures/<language>/<bank>/<doctype>[.<tag>].txt

Folder names are the exact ``.value`` of :class:`Language` and :class:`BankId`;
the filename stem (minus the ``.txt`` suffix, and minus any optional ``.<tag>``
disambiguator) is the exact ``.value`` of :class:`DocumentType`. That way the
path itself declares the fixture's expected classification, and adding a new
fixture is a drag-and-drop operation that automatically picks up test coverage
via :func:`discover_fixtures`.

Examples::

    en/pictet/redemption_notice.txt                 # single sample per doctype
    en/pictet/redemption_notice.anonymised.txt      # additional sample, same doctype
    es/pictet/subscription_notice.txt               # Spanish counterpart

"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from banking_pipeline.models import BankId, DocumentType, Language, RawDocument

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@dataclass(frozen=True)
class FixtureCase:
    """A fixture file plus the classification its location declares."""

    path: Path
    language: Language
    bank: BankId
    doctype: DocumentType

    @property
    def relative_path(self) -> Path:
        return self.path.relative_to(FIXTURES_DIR)

    def load(self) -> RawDocument:
        return RawDocument(
            path=self.path,
            text=self.path.read_text(encoding="utf-8"),
            page_count=1,
        )


def discover_fixtures(root: Path = FIXTURES_DIR) -> list[FixtureCase]:
    """Walk ``root`` and yield a :class:`FixtureCase` for every ``*.txt`` file
    at ``<lang>/<bank>/<file>.txt`` depth. The doctype is parsed from the
    filename stem: everything up to the first ``.`` (so
    ``redemption_notice.anonymised.txt`` → doctype=REDEMPTION_NOTICE, tag
    ``anonymised``). Files whose parent folders or filename prefix don't map
    cleanly to enum values are skipped rather than crashing; a dedicated test
    asserts every ``.txt`` actually resolves to a valid triple.
    """

    cases: list[FixtureCase] = []
    if not root.is_dir():
        return cases

    for txt_path in sorted(root.rglob("*.txt")):
        rel_parts = txt_path.relative_to(root).parts
        if len(rel_parts) != 3:
            # Not a <lang>/<bank>/<file> layout — skip.
            continue
        lang_name, bank_name, filename = rel_parts
        # Strip the ``.txt`` suffix, then take the portion before the first
        # ``.`` as the doctype (everything after ``.`` is a free-form tag).
        doctype_name = filename.removesuffix(".txt").split(".", 1)[0]
        try:
            language = Language(lang_name)
            bank = BankId(bank_name)
            doctype = DocumentType(doctype_name)
        except ValueError:
            continue
        cases.append(FixtureCase(path=txt_path, language=language, bank=bank, doctype=doctype))

    return cases


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def load_fixture_doc() -> callable[[str], RawDocument]:  # type: ignore[valid-type]
    """Return a loader that wraps a fixture ``.txt`` file as a :class:`RawDocument`.

    The argument is a path relative to ``tests/fixtures/`` — e.g.
    ``"en/pictet/redemption_notice.txt"``.
    """

    def _loader(relative_path: str) -> RawDocument:
        path = FIXTURES_DIR / relative_path
        return RawDocument(
            path=path, text=path.read_text(encoding="utf-8"), page_count=1
        )

    return _loader


@pytest.fixture
def fixture_cases() -> list[FixtureCase]:
    """All discovered fixture cases. Handy for ad-hoc tests; parametric
    tests should call :func:`discover_fixtures` directly in their
    ``@pytest.mark.parametrize`` decorator because pytest fixtures can't be
    consumed by parametrize."""

    return discover_fixtures()
