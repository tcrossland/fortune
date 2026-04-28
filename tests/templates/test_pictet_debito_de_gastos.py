from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetDebitoDeGastosTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_debito_de_gastos_template_is_registered() -> None:
    assert "pictet.debito_de_gastos.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.debito_de_gastos.v1"]
    assert template.template_id == "pictet.debito_de_gastos.v1"


def test_debito_de_gastos_extracts_single_transaction() -> None:
    template = PictetDebitoDeGastosTemplate()
    txs = template.extract(_load("debito_de_gastos.txt"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.trade_date == date(2023, 3, 20)
    assert tx.settlement_date == date(2023, 3, 31)
    assert tx.booking_date == date(2023, 3, 20)
    assert tx.currency == "EUR"
    assert tx.amount == Decimal("-3488.36")
    assert tx.isin is None
    assert tx.quantity is None
    assert tx.price is None
    # Narration uses the ``Período`` range formatted in Pictet's printed
    # dd.mm.yyyy form — matching the convention pinned by the
    # ``debito_de_gastos.2021`` golden file. The free-form ``Comentario``
    # line is no longer surfaced through the narration; it was redundant
    # with the period range and forced the narration shape to vary
    # between fixtures that did and didn't carry one.
    assert tx.narration == "Periodo 01.01.2023 - 31.03.2023"
    assert tx.title == "Débito de gastos"
    assert tx.transaction_number == "855093717"
    assert tx.account_number == "P-999999.999"
    # The 2023 fixture has multi-line fee labels (``Honorarios de`` +
    # ``administración`` + ``EUR -3'088.36``); ``find_fee_breakdown``
    # now joins the wrapped parts with single spaces and emits one
    # ``FeeItem`` per row, same way it handles the EN
    # ``debit_of_fees`` advice.
    assert len(tx.fee_breakdown) == 2
    assert tx.fee_breakdown[0].description == "Honorarios de administración"
    assert tx.fee_breakdown[0].amount == Decimal("-3088.36")
    assert tx.fee_breakdown[0].currency == "EUR"
    assert tx.fee_breakdown[1].description == "Comisiones de mantenimiento de cuenta"
    assert tx.fee_breakdown[1].amount == Decimal("-400.00")
