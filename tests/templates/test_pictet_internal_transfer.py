from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetInternalTransferTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_internal_transfer_template_is_registered() -> None:
    assert "pictet.internal_transfer.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.internal_transfer.v1"]
    assert template.template_id == "pictet.internal_transfer.v1"


def test_internal_transfer_extracts_single_cross_currency_transaction() -> None:
    """The template now produces ONE Transaction with both legs' info,
    not two separate Transactions. The writer's
    ``_render_internal_transfer`` path uses ``counter_currency`` /
    ``counter_amount`` to emit a single beancount entry with an ``@@``
    annotation linking the two cash currencies."""

    template = PictetInternalTransferTemplate()
    txs = template.extract(_load("internal_transfer.txt"))

    assert len(txs) == 1
    tx = txs[0]

    # Fixture is fully anonymised (digits → 9, dates → 01.01.2022) but
    # the structural sign convention is preserved.
    assert tx.trade_date == date(2022, 1, 1)
    assert tx.settlement_date == date(2022, 1, 1)
    assert tx.booking_date == date(2022, 1, 1)

    # Source (debit) leg: signed negative — cash leaving the EUR account.
    assert tx.currency == "EUR"
    assert tx.amount == Decimal("-99999.99")

    # Destination (credit) leg: signed positive — cash arriving in the
    # GBP account. Pictet performs the FX inside the leg, so the Net
    # amount on the credit side is already in the destination currency.
    assert tx.counter_currency == "GBP"
    assert tx.counter_amount == Decimal("99999.99")

    # Synthesised narration — the document carries no verb-led
    # headline, so the template builds one from the leg pair.
    assert tx.narration == "EUR → GBP"
    assert tx.title == "Internal money transfer"
    assert tx.transaction_number == "9999999999"
    assert tx.exchange_rate == Decimal("9.99999999")
    # Account number falls back to the portfolio header (the IBAN is
    # anonymised and won't validate).
    assert tx.account_number == "K-999999.999"


def test_internal_transfer_template_rejects_outgoing_payment() -> None:
    """An outgoing payment shares the ``PAYMENT TRANSACTIONS`` banner
    but lacks the ``Internal money transfer`` title; the template must
    bail rather than try to interpret a single-leg payment as a
    cross-currency transfer."""

    template = PictetInternalTransferTemplate()
    txs = template.extract(_load("payment.txt"))
    assert txs == []
