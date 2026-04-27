"""Language-detection tests.

The rule-based detector is dependency-free (stopword counts) so these tests
don't need the Anthropic SDK or a network. The LLM-fallback path is
exercised implicitly via the hybrid wrapper only when an API key is set;
here we test the rules tier directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from banking_pipeline.classifiers.language import (
    LanguageClassifier,
    LanguageRuleClassifier,
)
from banking_pipeline.config import settings
from banking_pipeline.models import Language, RawDocument
from tests.conftest import FixtureCase, discover_fixtures


def _doc(text: str) -> RawDocument:
    return RawDocument(path=Path("snippet.txt"), text=text, page_count=1)


# --- English ---------------------------------------------------------------

ENGLISH_PARAGRAPH = (
    "This is a confirmation that the order has been executed on the exchange. "
    "The proceeds will be credited to your account in the next working day, "
    "and a further advice will be sent when the settlement is complete."
)


def test_english_paragraph_detects_english() -> None:
    result = LanguageRuleClassifier().classify(_doc(ENGLISH_PARAGRAPH))
    assert result.language is Language.ENGLISH
    assert result.confidence > 0.7


def test_english_fixture_detects_english(load_fixture_doc) -> None:  # type: ignore[no-untyped-def]
    """Spot-check the prose-poor structured-advice case: classic frequency
    stopwords alone barely score on these, so the banking-domain tokens in
    ``ENGLISH_STOPWORDS`` are doing the heavy lifting here."""
    for fixture in (
        "en/pictet/redemption_notice.txt",
        "en/pictet/subscription_notice.txt",
    ):
        result = LanguageRuleClassifier().classify(load_fixture_doc(fixture))
        assert result.language is Language.ENGLISH, fixture


# --- Spanish ---------------------------------------------------------------

SPANISH_PARAGRAPH = (
    "Esta es una confirmación de que la orden ha sido ejecutada en el mercado. "
    "El importe será abonado en su cuenta al día hábil siguiente, y se enviará "
    "un aviso adicional cuando la liquidación esté completa. Los fondos están "
    "disponibles para su uso entre las partes del contrato."
)


def test_spanish_paragraph_detects_spanish() -> None:
    result = LanguageRuleClassifier().classify(_doc(SPANISH_PARAGRAPH))
    assert result.language is Language.SPANISH
    assert result.confidence > 0.5


# --- Edge cases ------------------------------------------------------------

def test_empty_text_is_unknown() -> None:
    result = LanguageRuleClassifier().classify(_doc(""))
    assert result.language is Language.UNKNOWN
    assert result.confidence == 0.0


def test_numbers_only_is_unknown() -> None:
    """Pure numeric noise — no stopwords hit at all, should read as unknown."""
    result = LanguageRuleClassifier().classify(
        _doc("123456 789.00 EUR 2025-10-20 K-000000.001")
    )
    assert result.language is Language.UNKNOWN


def test_hybrid_wrapper_returns_rules_when_threshold_met() -> None:
    """Without an API key, the hybrid classifier should return the rules
    result verbatim — no silent fallback attempts."""
    result = LanguageClassifier().classify(_doc(ENGLISH_PARAGRAPH))
    assert result.language is Language.ENGLISH
    assert result.source == "rules"


# --- Per-fixture regression guard ------------------------------------------

# Every non-empty Pictet fixture should clear the rule-confidence threshold
# unaided by the LLM — the domain tokens added to both stopword lists were
# chosen specifically so these structured-advice documents score high enough
# that no network call is ever needed on in-corpus inputs. If a change to
# the stopword lists (or the confidence shaping) drops any fixture below
# ``settings.rule_confidence_threshold``, this test pinpoints which one.

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
def test_rules_tier_clears_threshold_on_every_fixture(case: FixtureCase) -> None:
    result = LanguageRuleClassifier().classify(case.load())
    assert result.language is case.language, case.relative_path
    assert result.confidence >= settings.rule_confidence_threshold, (
        f"{case.relative_path}: confidence {result.confidence:.3f} "
        f"below threshold {settings.rule_confidence_threshold}"
    )
