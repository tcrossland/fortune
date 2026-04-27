"""Regression tests against real (anonymised) Pictet correspondence.

These fixtures live in ``tests/fixtures/`` and are the canonical smoke set for
the layered classifier. Add more per-bank fixtures next to these as new bank
rulesets land.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from banking_pipeline.classifiers.bank import BankRuleClassifier
from banking_pipeline.classifiers.hybrid import LayeredClassifier
from banking_pipeline.config import settings
from banking_pipeline.models import BankId, DocumentType, Language, RawDocument
from tests.conftest import FixtureCase, discover_fixtures


def test_redemption_fixture_classifies_as_pictet_redemption(load_fixture_doc) -> None:  # type: ignore[no-untyped-def]
    doc = load_fixture_doc("en/pictet/redemption_notice.txt")
    result = LayeredClassifier().classify(doc)

    assert result.language is not None
    assert result.language.language is Language.ENGLISH
    assert result.bank is not None
    assert result.bank.bank is BankId.PICTET
    assert result.bank.confidence > 0.3
    assert result.document_type is DocumentType.REDEMPTION_NOTICE
    assert result.template_id == "pictet.redemption_notice.v1"


def test_subscription_fixture_classifies_as_pictet_subscription(load_fixture_doc) -> None:  # type: ignore[no-untyped-def]
    doc = load_fixture_doc("en/pictet/subscription_notice.txt")
    result = LayeredClassifier().classify(doc)

    assert result.language is not None
    assert result.language.language is Language.ENGLISH
    assert result.bank is not None
    assert result.bank.bank is BankId.PICTET
    assert result.bank.confidence > 0.3
    assert result.document_type is DocumentType.SUBSCRIPTION_NOTICE
    assert result.template_id == "pictet.subscription_notice.v1"


def test_bank_identification_works_without_letterhead(load_fixture_doc) -> None:  # type: ignore[no-untyped-def]
    """Strip the Pictet letterhead markers from the fixture and confirm the
    structural quirks alone (``CASH EFFECTin portfolio``, the ``K-NNNNNN.NNN``
    account format, ``Telekurs ID``, ``IBAN?LU``) still identify the bank.
    This is the whole point of the structural markers in ``BANK_RULES``.
    """
    original = load_fixture_doc("en/pictet/redemption_notice.txt")
    redacted_text = re.sub(r"(?i)\bpictet\b", "REDACTED", original.text)
    redacted_text = redacted_text.replace("PICTCHGG", "XXXXXXXX")
    assert "Pictet" not in redacted_text  # sanity: letterhead is gone
    redacted = RawDocument(
        path=Path("redemption.txt"), text=redacted_text, page_count=1
    )

    result = BankRuleClassifier().classify(redacted)
    assert result.bank is BankId.PICTET


# --- Per-fixture regression guard for the bank stage -----------------------

# Every non-empty fixture in the tree is a Pictet document and should be
# identifiable as such by the rules tier alone — the point of the expanded
# pattern set (Luxembourg letterhead, Madrid letterhead, pictet.com domain,
# 0A08 reference prefix, broadened account format) is exactly that the
# non-security advices (interest_scale, debit_of_fees, invoices, etc.)
# score enough independent signatures to clear the LLM-fallback threshold
# without calling the network. If a change to ``BANK_RULES`` drops any
# fixture below ``settings.rule_confidence_threshold``, this test names
# which one.

_NON_EMPTY_CASES = [
    c for c in discover_fixtures() if c.path.read_text(encoding="utf-8").strip()
]


@pytest.mark.skipif(
    not _NON_EMPTY_CASES, reason="No non-empty fixtures discovered"
)
@pytest.mark.parametrize(
    "case",
    _NON_EMPTY_CASES,
    ids=[str(c.relative_path) for c in _NON_EMPTY_CASES],
)
def test_bank_rules_clear_threshold_on_every_fixture(case: FixtureCase) -> None:
    result = BankRuleClassifier().classify(case.load())
    assert result.bank is case.bank, case.relative_path
    assert result.confidence >= settings.rule_confidence_threshold, (
        f"{case.relative_path}: bank confidence {result.confidence:.3f} "
        f"below threshold {settings.rule_confidence_threshold}"
    )
