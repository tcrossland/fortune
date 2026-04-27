from decimal import Decimal
from pathlib import Path

from banking_pipeline.fields.regex_extract import RegexExtractor
from banking_pipeline.models import RawDocument


def test_extracts_amount_and_date() -> None:
    text = (
        "Trade Confirmation\n"
        "ISIN: US0378331005\n"
        "Trade Date: 2026-04-15\n"
        "Net Amount: USD 1,234.56\n"
    )
    doc = RawDocument(path=Path("t.pdf"), text=text, page_count=1)
    txs, confidence = RegexExtractor().extract(doc)

    assert len(txs) == 1
    tx = txs[0]
    assert tx.amount == Decimal("1234.56")
    assert tx.currency == "USD"
    assert tx.isin == "US0378331005"
    assert confidence > 0.5
