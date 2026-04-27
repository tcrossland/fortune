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


def test_internal_transfer_extracts_two_legs() -> None:
    template = PictetInternalTransferTemplate()
    txs = template.extract(_load("internal_transfer.txt"))

    assert len(txs) == 2
    debit, credit = txs

    # Fixture is fully anonymised (digits → 9, dates → 01.01.2022) but
    # the structural split is preserved, so we can pin the leg sign
    # convention end-to-end.
    assert debit.trade_date == date(2022, 1, 1)
    assert debit.settlement_date == date(2022, 1, 1)
    assert debit.currency == "EUR"
    assert debit.amount == Decimal("-99999.99")

    # Credit leg's currency is GBP because Pictet performs the FX inside
    # the leg — Net amount is in the destination currency, not in the
    # source's. This is the load-bearing detail that distinguishes
    # internal transfers from payments.
    assert credit.currency == "GBP"
    assert credit.amount == Decimal("99999.99")

    assert debit.narration == credit.narration
    assert "internal transfer" in debit.narration
    assert "EUR" in debit.narration
    assert "GBP" in debit.narration


def test_internal_transfer_template_rejects_outgoing_payment() -> None:
    """An outgoing payment shares the ``PAYMENT TRANSACTIONS`` banner
    but lacks the ``Internal money transfer`` title; the template must
    bail rather than try to interpret a single-leg payment as a
    cross-currency transfer."""

    template = PictetInternalTransferTemplate()
    txs = template.extract(_load("payment.txt"))
    assert txs == []
