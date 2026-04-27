"""Two-stage classifier: bank first, then document type scoped to that bank."""

from pathlib import Path

from banking_pipeline.classifiers.hybrid import TwoStageClassifier
from banking_pipeline.models import BankId, DocumentType, RawDocument


def _doc(text: str) -> RawDocument:
    return RawDocument(path=Path("sample.pdf"), text=text, page_count=1)


def test_pictet_redemption_routes_via_bank_then_doc_type() -> None:
    text = (
        "Banque Pictet & Cie\n"
        "Redemption\n"
        "Operation type Sale\n"
        "Executed quantity -165\n"
        "Execution price EUR 205.24\n"
        "OUT of portfolio P-999999.999\n"
    )
    result = TwoStageClassifier().classify(_doc(text))

    assert result.bank is not None
    assert result.bank.bank is BankId.PICTET
    assert result.bank.confidence > 0.0
    assert result.document_type is DocumentType.REDEMPTION_NOTICE
    assert result.template_id == "pictet.redemption_notice.v1"


def test_generic_trade_confirmation_does_not_match_pictet_rules() -> None:
    text = (
        "Trade Confirmation\n"
        "BOUGHT 100 shares\n"
        "ISIN: US0378331005\n"
    )
    result = TwoStageClassifier().classify(_doc(text))

    assert result.bank is not None
    # No bank letterhead in this sample, so bank stage reports UNKNOWN.
    assert result.bank.bank is BankId.UNKNOWN
    # But the generic rules still fire.
    assert result.document_type is DocumentType.TRADE_CONFIRMATION


def test_pictet_phrases_do_not_leak_when_bank_is_unknown() -> None:
    # Contains a Pictet-specific phrase but no bank identity. The two-stage
    # classifier should refuse to use the Pictet ruleset and therefore not
    # classify this as a redemption notice.
    text = (
        "Executed quantity 100\n"
        "Execution price EUR 100\n"
        "OUT of portfolio X-1\n"
    )
    result = TwoStageClassifier().classify(_doc(text))

    assert result.bank is not None
    assert result.bank.bank is BankId.UNKNOWN
    assert result.document_type is not DocumentType.REDEMPTION_NOTICE
