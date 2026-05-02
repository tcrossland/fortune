"""Parametric collector for every (fixture, golden) pair under tests/fixtures.

Replaces the bulk of the per-doctype ``tests/templates/test_*_golden.py``
files with one self-discovering test that walks the fixtures tree
(via :func:`tests.conftest.discover_fixtures`) and parametrises over
every ``<name>.txt`` that has a sibling ``<name>.beancount`` golden.

The naming convention is documented in ``tests/conftest.py`` —
``<lang>/<bank>/<doctype>[.<tag>].txt``. The doctype is the prefix
before the first ``.`` in the filename, so ``buy_shares.2025``,
``suscripcion.fx``, and ``switch_entrada.202308`` all resolve to
their parent doctype enum value.

Why a collector
---------------
Every per-template golden test followed the same five-step shape:
load fixture → build classification → run template → render →
compare. With 30+ near-identical files the pattern was pure
boilerplate, and the project's own README already promised "drop a
fixture, get a test for free" — which wasn't true until this
collector existed. Adding a new fixture is now zero-code: drop
``<doctype>.txt`` + ``<doctype>.beancount`` (or
``<doctype>.<tag>.txt`` + ``<doctype>.<tag>.beancount``) under the
right ``<lang>/<bank>/`` directory and it auto-registers.

What's NOT covered
------------------
Fixtures that need bespoke setup (e.g. a ``link_id`` patch to
demonstrate cross-leg pairing, or a date substitution because the
fixture is anonymised to ``99.99.9999``) are listed in
:data:`_BESPOKE_FIXTURES` and skipped here; they keep their own
``test_*_golden.py`` file.

Fixtures with no sibling ``.beancount`` (statements, ``factura``,
``fx_forward`` opening, etc. — doctypes that intentionally produce
no output) are filtered out at discovery time, no skip needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from banking_pipeline import beancount_writer
from banking_pipeline.models import (
    BankClassification,
    Classification,
    LanguageClassification,
)
from banking_pipeline.templates import TEMPLATE_REGISTRY
from tests.conftest import FIXTURES_DIR, FixtureCase, discover_fixtures

# Fixtures that need bespoke setup the collector can't replicate.
# Keep this list small and well-justified; each entry must point at a
# dedicated ``test_*_golden.py`` that covers the fixture. Paths are
# relative to ``tests/fixtures/`` and stripped of the ``.txt`` suffix.
_BESPOKE_FIXTURES: frozenset[str] = frozenset({
    # Patches ``link_id`` onto the entrada to demonstrate the
    # cross-leg link to its paired salida — see
    # tests/templates/test_pictet_switch_entrada_golden.py.
    "es/pictet/switch_entrada.2021",
    # Date fields anonymised to ``99.99.9999`` (which datetime.date
    # rejects). The dedicated test substitutes ``30.06.2026`` before
    # extraction — see tests/templates/test_pictet_pago_interna_golden.py.
    "es/pictet/pago_interna",
})


def _is_bespoke(case: FixtureCase) -> bool:
    """True when the fixture is in :data:`_BESPOKE_FIXTURES`."""

    rel = str(case.relative_path.with_suffix(""))
    return rel in _BESPOKE_FIXTURES


def _has_golden(case: FixtureCase) -> bool:
    """True when the fixture has a sibling ``.beancount`` file."""

    return case.path.with_suffix(".beancount").exists()


def _collect_renderable_cases() -> list[FixtureCase]:
    """Return every fixture eligible for the parametric render check.

    Filters :func:`discover_fixtures` to the cases that (1) have a
    sibling ``.beancount`` golden and (2) aren't on the bespoke skip
    list. Sorted by relative path for stable test-id ordering.
    """

    return sorted(
        (
            case
            for case in discover_fixtures()
            if _has_golden(case) and not _is_bespoke(case)
        ),
        key=lambda c: str(c.relative_path),
    )


_CASES = _collect_renderable_cases()


@pytest.mark.parametrize(
    "case",
    _CASES,
    ids=[str(c.relative_path.with_suffix("")) for c in _CASES],
)
def test_render_matches_golden(case: FixtureCase) -> None:
    """Render the first transaction extracted from ``case.path`` and
    compare against its sibling ``.beancount`` golden, byte-for-byte.

    Looks up the template via ``TEMPLATE_REGISTRY`` keyed on
    ``<bank>.<doctype>.v1``. A missing template surfaces as a fixture
    error rather than a render mismatch — distinct failure mode that
    the test name makes clear.
    """

    template_id = f"{case.bank.value}.{case.doctype.value}.v1"
    template = TEMPLATE_REGISTRY.get(template_id)
    assert template is not None, (
        f"No template registered for {template_id} "
        f"(fixture {case.relative_path}); register the template under "
        "TEMPLATE_REGISTRY or add the fixture's path to "
        "_BESPOKE_FIXTURES with a comment pointing at the test that "
        "covers it."
    )

    txs = template.extract(case.load())
    assert len(txs) == 1, (
        f"Expected exactly one Transaction from {case.relative_path}, "
        f"got {len(txs)}. Multi-transaction goldens need bespoke "
        "handling — add the fixture to _BESPOKE_FIXTURES."
    )

    classification = Classification(
        document_type=case.doctype,
        confidence=0.95,
        source="rules",
        template_id=template_id,
        bank=BankClassification(
            bank=case.bank, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=case.language, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden_path = case.path.with_suffix(".beancount")
    expected = golden_path.read_text(encoding="utf-8")

    assert rendered == expected, (
        f"Rendered output for {case.relative_path} doesn't match "
        f"{golden_path.name}.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{expected}"
    )


def test_collector_discovered_at_least_one_fixture() -> None:
    """Sanity check — if ``_collect_renderable_cases()`` returns zero
    cases the parametrize set is empty and pytest silently passes,
    which would mask a refactor that broke discovery. This guard
    makes that failure mode loud."""

    assert _CASES, (
        "Parametric golden collector found zero fixture/golden pairs. "
        f"Check {FIXTURES_DIR} layout, the bespoke-skip list, and the "
        "doctype-stem split in tests/conftest.py:discover_fixtures."
    )
