from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline import beancount_writer
from banking_pipeline.models import (
    Classification,
    DocumentType,
    ExtractionResult,
    Transaction,
)


def test_render_trade_confirmation_has_date_and_isin() -> None:
    tx = Transaction(
        trade_date=date(2026, 4, 15),
        narration="BUY AAPL",
        currency="USD",
        amount=Decimal("1234.56"),
        isin="US0378331005",
        quantity=Decimal("10"),
        price=Decimal("123.456"),
        source_path=Path("t.pdf"),
    )
    result = ExtractionResult(
        classification=Classification(
            document_type=DocumentType.TRADE_CONFIRMATION,
            confidence=0.9,
            source="rules",
            template_id="generic.trade_confirmation.v1",
        ),
        transactions=[tx],
        source_path=Path("t.pdf"),
    )
    rendered = beancount_writer.render(result)
    assert "2026-04-15" in rendered
    assert "US0378331005" in rendered
    assert "USD" in rendered
