from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetPagoInternaTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str, *, date_substitute: str | None = None) -> RawDocument:
    """Load a fixture, optionally swapping in a valid date for fixtures
    whose date fields are fully anonymised.

    The pago_interna fixture is anonymised more aggressively than the rest
    of the ES set: its date fields are ``99.99.9999`` which Python's
    :class:`datetime.date` rejects (months are 1..12). Substituting a real
    date keeps the template parseable while preserving every other
    structural detail of the fixture (account format, narration shape,
    cash-effect block).
    """

    path = FIXTURES / name
    text = path.read_text(encoding="utf-8")
    if date_substitute is not None:
        text = text.replace("99.99.9999", date_substitute)
    return RawDocument(path=path, text=text, page_count=1)


def test_pago_interna_template_is_registered() -> None:
    assert "pictet.pago_interna.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.pago_interna.v1"]
    assert template.template_id == "pictet.pago_interna.v1"


def test_pago_interna_extracts_single_transaction() -> None:
    template = PictetPagoInternaTemplate()
    txs = template.extract(_load("pago_interna.txt", date_substitute="30.06.2026"))

    assert len(txs) == 1
    tx = txs[0]
    assert tx.trade_date == date(2026, 6, 30)
    assert tx.settlement_date == date(2026, 6, 30)
    assert tx.currency == "EUR"
    # Fixture's anonymised positive amount; sign matches an incoming wire.
    assert tx.amount == Decimal("9999.99")
    assert tx.isin is None
    # ``Ordenante`` (instructing party) carried into narration.
    assert "FIRSTNAMES LASTNAMES" in tx.narration
    # ``Comentario`` block carried into narration.
    assert "SENT FROM REVOLUT" in tx.narration
    # K- prefix because this is the Luxembourg-issued ES advice (the
    # other ES fixtures use P-).
    assert tx.account_number == "K-999999.999"
