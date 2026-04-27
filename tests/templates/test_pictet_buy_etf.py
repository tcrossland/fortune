from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetBuyEtfTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_buy_etf_template_is_registered() -> None:
    assert "pictet.buy_etf.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.buy_etf.v1"]
    assert template.template_id == "pictet.buy_etf.v1"


def test_buy_etf_extracts_single_transaction() -> None:
    template = PictetBuyEtfTemplate()
    txs = template.extract(_load("buy_etf.txt"))

    assert len(txs) == 1
    tx = txs[0]
    # Dates are the only de-anonymised values in the fixture; everything
    # else is masked with 9s. The fixture's masked numerics are still
    # parseable (Swiss apostrophe digits) so amount / price / quantity
    # surface cleanly even though the values aren't realistic.
    assert tx.trade_date == date(2026, 4, 9)
    assert tx.settlement_date == date(2026, 4, 13)
    assert tx.currency == "EUR"
    # ``amount`` is the printed ``Net amount`` line, not gross+costs computed
    # from the cost block. In a real advice these differ (net = gross + costs);
    # the fixture's masking happened to set both to -999'999.99 so the cost
    # subtraction is invisible here, but the parser would surface a real net
    # untouched on real PDFs.
    assert tx.amount == Decimal("-999999.99")
    assert tx.quantity == Decimal("9999")
    assert tx.price == Decimal("999.9999")
    # The fixture's ``ISIN/Internal ref.`` is ``LU9999999999`` — passes the
    # 12-contiguous-char regex but fails the LU checksum, so ``resolve_isin``
    # returns the raw value rather than dropping it. Real ETFs carry valid
    # ISINs that round-trip through the validator.
    assert tx.isin == "LU9999999999"
    # Pictet's headline-verb pattern catches the "Buy 9'999 Multi Units ..."
    # line near the top of every advice; the AMUNDI fund name is preserved.
    assert "AMUNDI" in tx.narration.upper()
    # The fixture's IBAN won't pass checksum validation, so resolve_account_number
    # falls back to Pictet's internal portfolio identifier.
    assert tx.account_number == "K-999999.999"
