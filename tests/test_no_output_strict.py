"""Companion / disclosure doctypes are strict-safe no-output documents.

These templates return ``[]`` by design (the cash leg lives on a sibling
advice, or there's no cash event at all), so their doctype must be in
``NO_OUTPUT_DOCTYPES`` — otherwise ``HybridExtractor(strict=True)`` treats
the empty result as a template regression and raises, breaking
``ingest --strict`` / ``rebuild --strict`` on a perfectly good document.
Regression guard for exactly that gap.
"""

from __future__ import annotations

import pytest

from banking_pipeline.fields import HybridExtractor
from banking_pipeline.models import (
    NO_OUTPUT_DOCTYPES,
    BankClassification,
    BankId,
    Classification,
    DocumentType,
    Language,
    LanguageClassification,
)

# (fixture path, doctype, template_id, language)
_CASES = [
    ("es/pictet/factura.txt", DocumentType.FACTURA, "pictet.factura.v1", Language.SPANISH),
    (
        "en/pictet/interest_scale.txt",
        DocumentType.INTEREST_SCALE,
        "pictet.interest_scale.v1",
        Language.ENGLISH,
    ),
    (
        "en/pictet/order_information_report.txt",
        DocumentType.ORDER_INFORMATION_REPORT,
        "pictet.order_information_report.v1",
        Language.ENGLISH,
    ),
]


@pytest.mark.parametrize(
    ("fixture", "doctype", "template_id", "language"),
    _CASES,
    ids=[c[1].value for c in _CASES],
)
def test_no_output_doctype_is_strict_safe(  # type: ignore[no-untyped-def]
    load_fixture_doc, fixture, doctype, template_id, language
) -> None:
    assert doctype in NO_OUTPUT_DOCTYPES

    doc = load_fixture_doc(fixture)
    classification = Classification(
        document_type=doctype,
        confidence=0.95,
        source="rules",
        template_id=template_id,
        bank=BankClassification(bank=BankId.PICTET, confidence=0.99, source="rules"),
        language=LanguageClassification(
            language=language, confidence=0.99, source="rules"
        ),
    )

    # Must not raise TemplateExtractionError under strict, and must emit
    # nothing.
    txs, _warnings = HybridExtractor(strict=True).extract(doc, classification)
    assert txs == []
