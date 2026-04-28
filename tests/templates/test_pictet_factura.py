from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetFacturaTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_factura_template_is_registered() -> None:
    assert "pictet.factura.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.factura.v1"]
    assert template.template_id == "pictet.factura.v1"


def test_factura_extracts_no_transactions() -> None:
    """The factura is the tax-invoice paper trail for the same fee event
    that a matching ``Débito de gastos`` advice books as a cash leg.
    Emitting both would double-count the fee, so this template
    intentionally returns ``[]`` (mirroring ``interest_scale``). The
    classifier still routes the document so audit logs see it."""

    template = PictetFacturaTemplate()
    txs = template.extract(_load("factura.txt"))
    assert txs == []
