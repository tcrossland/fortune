from pathlib import Path

from banking_pipeline.classifiers.rules import RuleClassifier
from banking_pipeline.models import DocumentType, RawDocument


def _doc(text: str) -> RawDocument:
    return RawDocument(path=Path("sample.pdf"), text=text, page_count=1)


def test_rules_identify_trade_confirmation() -> None:
    text = "Trade Confirmation\nBOUGHT 100 shares\nISIN: US0378331005"
    result = RuleClassifier().classify(_doc(text))
    assert result.document_type is DocumentType.TRADE_CONFIRMATION
    assert result.confidence > 0.5


def test_rules_return_unknown_when_no_match() -> None:
    result = RuleClassifier().classify(_doc("some unrelated text"))
    assert result.document_type is DocumentType.UNKNOWN
    assert result.confidence == 0.0
