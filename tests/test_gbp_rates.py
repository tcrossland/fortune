"""GBP cost-basis sourcing and pipeline-level enrichment."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.config import Settings
from banking_pipeline.fields.hybrid import HybridExtractor
from banking_pipeline.fx.gbp_rates import (
    EcbDailyRateSource,
    ForwardFillRateSource,
    HmrcMonthlyAverageSource,
    NullSource,
    build_rate_source,
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


def test_forward_fill_uses_exact_month_when_present() -> None:
    source = ForwardFillRateSource(HmrcMonthlyAverageSource.from_text(_CSV))
    assert source.get_rate(date(2023, 11, 24), "EUR") == Decimal("0.8696")
    assert source.get_rate(date(2023, 12, 31), "EUR") == Decimal("0.8625")


def test_forward_fill_falls_back_to_prior_month() -> None:
    """A month with no published rate marks to the latest earlier month —
    the month-end-snapshot-dated-to-the-1st boundary case."""

    source = ForwardFillRateSource(HmrcMonthlyAverageSource.from_text(_CSV))
    # Jan 2024 is absent; falls back to Dec 2023.
    assert source.get_rate(date(2024, 1, 1), "EUR") == Decimal("0.8625")
    # USD only exists in Nov 2023; Dec falls back to it.
    assert source.get_rate(date(2023, 12, 1), "USD") == Decimal("0.8013")


def test_forward_fill_bounded_lookback_edge() -> None:
    """Pins the 12-month cap exactly: the last EUR row is 2023-12, so a
    query exactly 12 months later still hits (query month + 12 prior), but
    13 months later falls past the window and surfaces as no rate."""

    source = ForwardFillRateSource(HmrcMonthlyAverageSource.from_text(_CSV))
    # 2024-12 is 12 months after 2023-12 → reached on the final look-back.
    assert source.get_rate(date(2024, 12, 1), "EUR") == Decimal("0.8625")
    # 2025-01 is 13 months out → past the cap.
    assert source.get_rate(date(2025, 1, 1), "EUR") is None


def test_forward_fill_of_null_source_is_none() -> None:
    """Wrapping a rateless source never fabricates a rate — the rate-gap
    reporting path is preserved."""

    source = ForwardFillRateSource(NullSource())
    assert source.get_rate(date(2023, 11, 24), "EUR") is None


def test_settings_default_disables_gbp_sourcing() -> None:
    # Assert the declared default, not the live singleton — the latter
    # picks up BANKPIPE_GBP_RATE_SOURCE from the environment / .env.
    assert Settings.model_fields["gbp_rate_source"].default == "null"


# --- ECB daily source ------------------------------------------------------

# GBP-per-1-unit, as the fetcher triangulates it. 1 Nov 2024 is a Friday.
_ECB_CSV = """date,currency,rate
2024-11-01,EUR,0.8300
2024-11-01,USD,0.7700
2024-11-04,EUR,0.8320
"""


def test_ecb_source_exact_dates_and_gbp_unit() -> None:
    s = EcbDailyRateSource.from_text(_ECB_CSV)
    assert s.get_rate(date(2024, 11, 1), "EUR") == Decimal("0.8300")
    assert s.get_rate(date(2024, 11, 4), "EUR") == Decimal("0.8320")
    assert s.get_rate(date(2024, 11, 1), "usd") == Decimal("0.7700")  # case-fold
    assert s.get_rate(date(2024, 11, 1), "GBP") == Decimal("1")


def test_ecb_source_weekend_walks_back_to_prior_publication() -> None:
    # Sunday 3 Nov 2024 has no fixing → the latest on/before it is Fri 1 Nov,
    # NOT the later Mon 4 Nov.
    s = EcbDailyRateSource.from_text(_ECB_CSV)
    assert s.get_rate(date(2024, 11, 3), "EUR") == Decimal("0.8300")


def test_ecb_source_unknown_currency_is_none() -> None:
    s = EcbDailyRateSource.from_text(_ECB_CSV)
    assert s.get_rate(date(2024, 11, 1), "JPY") is None


def test_ecb_source_gap_beyond_lookback_is_none() -> None:
    # USD exists only on 1 Nov; 20 Nov is far past the 7-day walk-back, so the
    # hole surfaces rather than silently reusing a stale rate.
    s = EcbDailyRateSource.from_text(_ECB_CSV)
    assert s.get_rate(date(2024, 11, 20), "USD") is None


def test_ecb_source_from_path(tmp_path: Path) -> None:
    p = tmp_path / "ecb.csv"
    p.write_text(_ECB_CSV, encoding="utf-8")
    s = EcbDailyRateSource.from_path(p)
    assert s.get_rate(date(2024, 11, 4), "EUR") == Decimal("0.8320")


def test_build_rate_source_selects_ecb(tmp_path: Path) -> None:
    p = tmp_path / "ecb.csv"
    p.write_text(_ECB_CSV, encoding="utf-8")
    s = build_rate_source(Settings(gbp_rate_source="ecb-daily", ecb_rate_path=p))
    assert isinstance(s, EcbDailyRateSource)
    assert s.get_rate(date(2024, 11, 1), "EUR") == Decimal("0.8300")


def test_build_rate_source_ecb_missing_file_degrades_to_null(tmp_path: Path) -> None:
    s = build_rate_source(
        Settings(gbp_rate_source="ecb-daily", ecb_rate_path=tmp_path / "nope.csv")
    )
    assert isinstance(s, NullSource)
