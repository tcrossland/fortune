"""GBP cost-basis sourcing and pipeline-level enrichment."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.config import settings
from banking_pipeline.fields.hybrid import HybridExtractor
from banking_pipeline.fx.gbp_rates import (
    HmrcMonthlyAverageSource,
    NullSource,
)
from banking_pipeline.models import (
    BankClassification,
    BankId,
    Classification,
    DocumentType,
    Language,
    LanguageClassification,
    Transaction,
)

_CSV = """month,currency,rate
2023-11,EUR,0.8696
2023-11,USD,0.8013
2023-12,EUR,0.8625
"""


def _tx(currency: str, *, on: date = date(2023, 11, 24)) -> Transaction:
    return Transaction(
        trade_date=on,
        narration="test",
        currency=currency,
        amount=Decimal("-100.00"),
        source_path=Path("test.txt"),
    )


def test_hmrc_source_snaps_date_to_month() -> None:
    source = HmrcMonthlyAverageSource.from_text(_CSV)
    # A mid-month date snaps to that month's row.
    assert source.get_rate(date(2023, 11, 24), "EUR") == Decimal("0.8696")
    assert source.get_rate(date(2023, 11, 1), "USD") == Decimal("0.8013")
    assert source.get_rate(date(2023, 12, 31), "EUR") == Decimal("0.8625")


def test_hmrc_source_missing_month_returns_none() -> None:
    source = HmrcMonthlyAverageSource.from_text(_CSV)
    assert source.get_rate(date(2024, 1, 15), "EUR") is None


def test_hmrc_source_unknown_currency_returns_none() -> None:
    source = HmrcMonthlyAverageSource.from_text(_CSV)
    assert source.get_rate(date(2023, 11, 24), "JPY") is None


def test_hmrc_source_from_path(tmp_path: Path) -> None:
    csv_path = tmp_path / "hmrc.csv"
    csv_path.write_text(_CSV, encoding="utf-8")
    source = HmrcMonthlyAverageSource.from_path(csv_path)
    assert source.get_rate(date(2023, 11, 10), "EUR") == Decimal("0.8696")


def test_pipeline_enriches_non_gbp_transaction(load_fixture_doc) -> None:  # type: ignore[no-untyped-def]
    """A EUR fixture (buy_bonds, trade date 2023-11-24) gets ``gbp_rate``
    populated from the configured source via the full extract path."""

    extractor = HybridExtractor(
        rate_source=HmrcMonthlyAverageSource.from_text(_CSV)
    )
    doc = load_fixture_doc("en/pictet/buy_bonds.txt")
    classification = Classification(
        document_type=DocumentType.BUY_BONDS,
        confidence=0.95,
        source="rules",
        template_id="pictet.buy_bonds.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.ENGLISH, confidence=0.99, source="rules"
        ),
    )

    txs, _warnings = extractor.extract(doc, classification)

    assert len(txs) == 1
    assert txs[0].currency == "EUR"
    assert txs[0].gbp_rate == Decimal("0.8696")


def test_missing_rate_leaves_gbp_rate_none() -> None:
    """A non-GBP transaction with no rate available stays ``None``;
    extraction must not fail."""

    extractor = HybridExtractor(rate_source=NullSource())
    txs = [_tx("EUR")]
    extractor._enrich_gbp_rates(txs)
    assert txs[0].gbp_rate is None


def test_gbp_transaction_always_unit_rate() -> None:
    """GBP cash legs are 1:1 regardless of what the source would return."""

    class _AlwaysSource:
        def get_rate(self, on_date: date, currency: str) -> Decimal:
            return Decimal("999")

    extractor = HybridExtractor(rate_source=_AlwaysSource())
    gbp = _tx("GBP")
    eur = _tx("EUR")
    extractor._enrich_gbp_rates([gbp, eur])

    assert gbp.gbp_rate == Decimal("1")
    # Sanity: the source IS consulted for non-GBP currencies.
    assert eur.gbp_rate == Decimal("999")


def test_settings_default_disables_gbp_sourcing() -> None:
    assert settings.gbp_rate_source == "null"
