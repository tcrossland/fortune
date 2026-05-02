from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import (
    PictetCambioDeDivisasAperturaTemplate,
    PictetCambioDeDivisasCierreTemplate,
    PictetCambioDeDivisasTemplate,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


# ---------------------------------------------------------------------------
# Spot — ``Cambio de divisas al contado``
# ---------------------------------------------------------------------------


def test_cambio_de_divisas_template_is_registered() -> None:
    assert "pictet.cambio_de_divisas.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.cambio_de_divisas.v1"]
    assert template.template_id == "pictet.cambio_de_divisas.v1"


def test_cambio_de_divisas_extracts_single_cross_currency_transaction() -> None:
    """ES-locale spot FX advice — same shape as the EN ``SPOT`` template:
    a single Transaction holds both legs (``currency``/``amount`` for the
    sold side, ``counter_currency``/``counter_amount`` for the bought
    side). Routed through the writer's internal-transfer builder, which
    emits a single beancount entry with an ``@@`` annotation."""

    template = PictetCambioDeDivisasTemplate()
    txs = template.extract(_load("cambio_de_divisas.txt"))

    assert len(txs) == 1
    tx = txs[0]

    assert tx.trade_date == date(2023, 7, 10)
    assert tx.settlement_date == date(2023, 7, 10)
    assert tx.booking_date == date(2023, 7, 10)

    # Sold leg (USD): signed negative — cash leaving the USD account.
    assert tx.currency == "USD"
    assert tx.amount == Decimal("-314751.92")

    # Bought leg (GBP): signed positive — cash arriving in the GBP
    # account.
    assert tx.counter_currency == "GBP"
    assert tx.counter_amount == Decimal("244984.31")

    assert tx.title == "Cambio de divisas al contado"
    assert tx.narration == "Venta USD -314'751.92 contra GBP a 1.284784"
    assert tx.transaction_number == "884472676"
    # Fixture's anonymised IBAN won't validate — falls back to the
    # portfolio identifier from the document header.
    assert tx.account_number == "K-123456.001"


def test_cambio_de_divisas_template_rejects_apertura_advice() -> None:
    """The forward-opening advice shares the ``MERCADO DE DIVISAS``
    banner but uses ``a plazo (apertura)`` rather than ``al contado``.
    The spot template must bail rather than try to extract zero-amount
    forward-opening legs as a spot trade."""

    template = PictetCambioDeDivisasTemplate()
    txs = template.extract(_load("cambio_de_divisas_apertura.txt"))
    assert txs == []


def test_cambio_de_divisas_template_rejects_cierre_advice() -> None:
    """Same rationale for the forward-settlement advice — it uses
    ``a plazo (cierre)``, not ``al contado``."""

    template = PictetCambioDeDivisasTemplate()
    txs = template.extract(_load("cambio_de_divisas_cierre.txt"))
    assert txs == []


# ---------------------------------------------------------------------------
# Forward opening — ``Cambio de divisas a plazo (apertura)``
# ---------------------------------------------------------------------------


def test_cambio_de_divisas_apertura_template_is_registered() -> None:
    assert "pictet.cambio_de_divisas_apertura.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.cambio_de_divisas_apertura.v1"]
    assert template.template_id == "pictet.cambio_de_divisas_apertura.v1"


def test_cambio_de_divisas_apertura_emits_no_transactions() -> None:
    """Forward openings have zero cash impact — the matching cierre
    advice books the cash leg at maturity. Mirrors the EN
    ``FX_FORWARD`` template, which is also a no-emit document."""

    template = PictetCambioDeDivisasAperturaTemplate()
    txs = template.extract(_load("cambio_de_divisas_apertura.txt"))
    assert txs == []


# ---------------------------------------------------------------------------
# Forward settlement — ``Cambio de divisas a plazo (cierre)``
# ---------------------------------------------------------------------------


def test_cambio_de_divisas_cierre_template_is_registered() -> None:
    assert "pictet.cambio_de_divisas_cierre.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.cambio_de_divisas_cierre.v1"]
    assert template.template_id == "pictet.cambio_de_divisas_cierre.v1"


def test_cambio_de_divisas_cierre_extracts_settlement_with_spread() -> None:
    """ES forward-settlement advice — same shape as the EN
    ``SETTLE_FX_FORWARD`` template: one Transaction with the
    fee-bearing leg on ``currency``/``amount``, the other leg on
    ``counter_currency``/``counter_amount``, and ``fees``/
    ``fees_currency`` carrying the forward spread (printed as
    ``Spread <CCY>`` rather than ``Forward spread <CCY>`` like the
    EN sibling)."""

    template = PictetCambioDeDivisasCierreTemplate()
    txs = template.extract(_load("cambio_de_divisas_cierre.txt"))

    assert len(txs) == 1
    tx = txs[0]

    assert tx.trade_date == date(2023, 7, 10)
    assert tx.settlement_date == date(2023, 7, 10)
    assert tx.booking_date == date(2023, 7, 10)

    # Fee-bearing leg: EUR. The fee comes out of the EUR side of the
    # trade — Pictet writes the spread as ``Spread EUR -2'225.71`` and
    # the EUR ``EFECTO CASH`` block reflects that with
    # ``Importe neto = Importe bruto + Costes = -741'978.87 +
    # -2'225.71 = -744'204.58``.
    assert tx.currency == "EUR"
    assert tx.amount == Decimal("-744204.58")

    # Counter leg: GBP, signed positive — cash arriving in the GBP
    # account at settlement.
    assert tx.counter_currency == "GBP"
    assert tx.counter_amount == Decimal("635000.00")

    # Forward spread, signed as printed (negative); the writer flips
    # sign for the expense posting.
    assert tx.fees == Decimal("-2225.71")
    assert tx.fees_currency == "EUR"

    assert tx.title == "Cambio de divisas a plazo (cierre)"
    assert tx.narration == "Compra GBP 635'000.00 contra EUR a 0.855819519497"
    assert tx.transaction_number == "884473488"
    assert tx.account_number == "K-123456.001"


def test_cambio_de_divisas_cierre_template_rejects_apertura_advice() -> None:
    """An apertura advice shares the ``a plazo`` qualifier but uses
    ``(apertura)`` rather than ``(cierre)`` — and has no ``Spread``
    cost line. The cierre template must bail."""

    template = PictetCambioDeDivisasCierreTemplate()
    txs = template.extract(_load("cambio_de_divisas_apertura.txt"))
    assert txs == []


def test_cambio_de_divisas_cierre_template_rejects_spot_advice() -> None:
    """A spot advice uses ``al contado``, not ``a plazo (cierre)``."""

    template = PictetCambioDeDivisasCierreTemplate()
    txs = template.extract(_load("cambio_de_divisas.txt"))
    assert txs == []
