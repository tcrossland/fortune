from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetSwitchEntradaTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_switch_entrada_template_is_registered() -> None:
    assert "pictet.switch_entrada.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.switch_entrada.v1"]
    assert template.template_id == "pictet.switch_entrada.v1"


def test_switch_entrada_extracts_single_transaction() -> None:
    template = PictetSwitchEntradaTemplate()
    txs = template.extract(_load("switch_entrada.txt"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.trade_date == date(2023, 8, 1)
    assert tx.settlement_date == date(2023, 8, 4)
    # No FX on this fund switch — both funds are EUR-denominated, so
    # the cash equivalent stays in EUR throughout.
    assert tx.currency == "EUR"
    assert tx.amount == Decimal("-44794.75")
    assert tx.quantity == Decimal("3940.078")
    assert tx.price == Decimal("11.369")
    assert tx.isin == "LU0767911984"
    # No verb-led headline on switches — falls back to the synthesised
    # narration.
    assert "switch" in tx.narration.lower()
    assert "entrada" in tx.narration.lower()
    # No ``Cuenta corriente`` line on switches; account_number falls
    # back to the ``N° de cuenta`` portfolio ID.
    assert tx.account_number == "P-999999.999"


def test_switch_entrada_rejects_salida_doc() -> None:
    """The two switch legs share most fields; the parenthesised
    ``(entrada)`` vs ``(salida)`` in the title is the discriminator."""

    template = PictetSwitchEntradaTemplate()
    txs = template.extract(_load("switch_salida.txt"))
    assert txs == []
