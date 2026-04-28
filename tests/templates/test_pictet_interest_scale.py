from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetInterestScaleTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_interest_scale_template_is_registered() -> None:
    assert "pictet.interest_scale.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.interest_scale.v1"]
    assert template.template_id == "pictet.interest_scale.v1"


def test_interest_scale_extracts_no_transactions() -> None:
    """The interest-scale advice is the per-day rate ledger that
    accompanies the matching ``INTEREST_PAYMENT`` cash-leg advice — both
    describe the same economic event. To avoid double-counting we emit
    a beancount entry only for the payment advice; the scale extractor
    intentionally returns ``[]``. The classifier still routes these
    documents correctly so audit/diagnostic logs see them."""

    template = PictetInterestScaleTemplate()
    txs = template.extract(_load("interest_scale.txt"))
    assert txs == []
