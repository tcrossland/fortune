from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetFxForwardTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_fx_forward_template_is_registered() -> None:
    assert "pictet.fx_forward.v1" in TEMPLATE_REGISTRY
    assert TEMPLATE_REGISTRY["pictet.fx_forward.v1"].template_id == "pictet.fx_forward.v1"


def test_fx_forward_extracts_no_transactions() -> None:
    """The FX-forward opening advice is the contractual paper trail
    for an event whose cash leg is booked by the matching
    ``SETTLE_FX_FORWARD`` advice at maturity. Emitting both would
    either double-count (with non-zero amounts) or clutter the ledger
    with zero-amount memo entries. This template intentionally
    returns ``[]`` (mirroring ``interest_scale`` and ``factura``).
    The classifier still routes the document so audit logs see it."""

    template = PictetFxForwardTemplate()
    txs = template.extract(_load("fx_forward.txt"))
    assert txs == []


def test_fx_forward_extracts_no_transactions_2026_fixture() -> None:
    """Same as above for the 2026 fixture — different portfolio
    identifier, identical structure. Pinning both to lock in the
    no-entry contract across the fixture-tree variants."""

    template = PictetFxForwardTemplate()
    txs = template.extract(_load("fx_forward.2026.txt"))
    assert txs == []
