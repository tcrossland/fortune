"""Nil-activity Vanguard statements are strict-safe; real regressions aren't.

A ``vanguard_regular_statement`` normally emits, so an empty extraction is
read as a regression and ``--strict`` raises. But a genuinely nil period
(a drained / £0 account whose statement carries no ``Activity`` section)
legitimately yields ``[]``. The template's optional ``is_expected_empty``
hook lets the hybrid extractor tell the two apart. These tests pin both
sides of that line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from banking_pipeline.fields import HybridExtractor, TemplateExtractionError
from banking_pipeline.models import (
    BankClassification,
    BankId,
    Classification,
    DocumentType,
    Language,
    LanguageClassification,
    RawDocument,
)
from banking_pipeline.templates.vanguard_uk.regular_statement import (
    VanguardRegularStatementTemplate,
)

_TEMPLATE_ID = "vanguard_uk.vanguard_regular_statement.v1"

# A statement with no ``Activity`` section — a nil-activity period.
_NIL_TEXT = (
    "Your Regular Statement\n"
    "Client name: Test User\n"
    "Account number: VG0000000\n"
    "Your Vanguard account summary\n"
    "ISA £0.00 £0.00\n"
    "Your cash and asset protection\n"
)

# A statement that DOES carry an ``Activity`` section but whose only row is
# a Bought line (owned by the contract notes, deliberately skipped) — so
# extraction is empty yet this is NOT a nil period.
_ACTIVITY_NO_BOOKABLE_TEXT = (
    "Your Regular Statement\n"
    "Account number: VG0000000\n"
    "Activity from 13 November 2025 to 12 February 2026 for your ISA\n"
    "Transaction date Transaction details Cash amount Cash balance\n"
    "13/02/2025 Bought 13 FTSE 250 UCITS ETF -£485.30 £514.70\n"
    "Your cash and asset protection\n"
)


def _doc(text: str) -> RawDocument:
    return RawDocument(path=Path("inbox/vg.pdf"), text=text, page_count=1)


def _classification() -> Classification:
    return Classification(
        document_type=DocumentType.VANGUARD_REGULAR_STATEMENT,
        confidence=0.95,
        source="rules",
        template_id=_TEMPLATE_ID,
        bank=BankClassification(
            bank=BankId.VANGUARD_UK, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.ENGLISH, confidence=0.99, source="rules"
        ),
    )


def test_is_expected_empty_true_without_activity_section() -> None:
    assert VanguardRegularStatementTemplate().is_expected_empty(_doc(_NIL_TEXT))


def test_is_expected_empty_false_with_activity_section() -> None:
    template = VanguardRegularStatementTemplate()
    assert not template.is_expected_empty(_doc(_ACTIVITY_NO_BOOKABLE_TEXT))


def test_nil_statement_is_strict_safe() -> None:
    txs, _warnings = HybridExtractor(strict=True).extract(
        _doc(_NIL_TEXT), _classification()
    )
    assert txs == []


def test_activity_section_but_empty_extraction_still_raises_under_strict() -> None:
    # The conservative boundary: an Activity section is present but yields
    # no bookable rows. That's left to the regression path — strict raises.
    with pytest.raises(TemplateExtractionError):
        HybridExtractor(strict=True).extract(
            _doc(_ACTIVITY_NO_BOOKABLE_TEXT), _classification()
        )
