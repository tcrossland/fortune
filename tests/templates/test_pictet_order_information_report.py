from pathlib import Path

from banking_pipeline.models import RawDocument
from banking_pipeline.templates import TEMPLATE_REGISTRY
from banking_pipeline.templates.pictet import PictetOrderInformationReportTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(path=path, text=path.read_text(encoding="utf-8"), page_count=1)


def test_order_information_report_template_is_registered() -> None:
    assert "pictet.order_information_report.v1" in TEMPLATE_REGISTRY
    template = TEMPLATE_REGISTRY["pictet.order_information_report.v1"]
    assert template.template_id == "pictet.order_information_report.v1"


def test_order_information_report_extracts_no_transactions() -> None:
    """The order information report is a pre-trade simulation — there is
    no historical economic event to extract. The template registers
    itself precisely so this case short-circuits the LLM fallback rather
    than letting it waste tokens trying to find a transaction that isn't
    there."""

    template = PictetOrderInformationReportTemplate()
    txs = template.extract(_load("order_information_report.txt"))
    assert txs == []
