"""Tests for ``render_close_directives``.

The function takes already-rendered beancount text and emits ``close``
directives for ISIN-keyed asset accounts whose final units balance is
exactly zero. Tests use hand-crafted beancount text rather than going
through the full extractor → writer chain because the close logic is
self-contained at the rendered-text layer.
"""

from __future__ import annotations

from banking_pipeline import beancount_writer


def test_buy_then_full_sell_emits_close() -> None:
    rendered = """\
2020-01-01 open Assets:Pic:Portfolio:US0378331005:USD  US0378331005

2024-03-01 * "BUY"
  Assets:Pic:Portfolio:US0378331005:USD  100 US0378331005 {123.45 USD}
  Assets:Pic:Cash:USD                   -12345.00 USD

2024-09-15 * "SELL"
  Assets:Pic:Portfolio:US0378331005:USD  -100 US0378331005 {} @ 150.00 USD
  Assets:Pic:Cash:USD                    15000.00 USD
  Income:Pic:Pnl:US0378331005           -2655.00 USD
"""
    closes = beancount_writer.render_close_directives(rendered)
    # Day after the last sell (2024-09-15 + 1 = 2024-09-16).
    assert "2024-09-16 close Assets:Pic:Portfolio:US0378331005:USD" in closes


def test_partial_sell_does_not_emit_close() -> None:
    rendered = """\
2020-01-01 open Assets:Pic:Portfolio:US0378331005:USD  US0378331005

2024-03-01 * "BUY"
  Assets:Pic:Portfolio:US0378331005:USD  100 US0378331005 {123.45 USD}
  Assets:Pic:Cash:USD                   -12345.00 USD

2024-09-15 * "PARTIAL SELL"
  Assets:Pic:Portfolio:US0378331005:USD  -50 US0378331005 {} @ 150.00 USD
  Assets:Pic:Cash:USD                    7500.00 USD
  Income:Pic:Pnl:US0378331005           -1327.50 USD
"""
    closes = beancount_writer.render_close_directives(rendered)
    assert closes == ""


def test_buy_sell_buy_not_closed() -> None:
    """A position that nets to non-zero across the batch (closed and then
    reopened) must not be closed — the later buy would be invalid."""

    rendered = """\
2020-01-01 open Assets:Pic:Portfolio:US0378331005:USD  US0378331005

2024-03-01 * "BUY"
  Assets:Pic:Portfolio:US0378331005:USD  100 US0378331005 {123.45 USD}
  Assets:Pic:Cash:USD                   -12345.00 USD

2024-06-01 * "FULL SELL"
  Assets:Pic:Portfolio:US0378331005:USD  -100 US0378331005 {} @ 150.00 USD
  Assets:Pic:Cash:USD                    15000.00 USD
  Income:Pic:Pnl:US0378331005           -2655.00 USD

2024-09-01 * "RE-BUY"
  Assets:Pic:Portfolio:US0378331005:USD  60 US0378331005 {140.00 USD}
  Assets:Pic:Cash:USD                   -8400.00 USD
"""
    closes = beancount_writer.render_close_directives(rendered)
    assert closes == ""


def test_multiple_isins_only_zeroed_one_closes() -> None:
    rendered = """\
2020-01-01 open Assets:Pic:Portfolio:US0378331005:USD  US0378331005
2020-01-01 open Assets:Pic:Portfolio:GB00B16KPT44:GBP  GB00B16KPT44

2024-03-01 * "BUY AAPL"
  Assets:Pic:Portfolio:US0378331005:USD  100 US0378331005 {123.45 USD}
  Assets:Pic:Cash:USD                   -12345.00 USD

2024-04-01 * "BUY VANGUARD UK"
  Assets:Pic:Portfolio:GB00B16KPT44:GBP  50 GB00B16KPT44 {200.00 GBP}
  Assets:Pic:Cash:GBP                   -10000.00 GBP

2024-09-15 * "SELL AAPL"
  Assets:Pic:Portfolio:US0378331005:USD  -100 US0378331005 {} @ 150.00 USD
  Assets:Pic:Cash:USD                    15000.00 USD
  Income:Pic:Pnl:US0378331005           -2655.00 USD
"""
    closes = beancount_writer.render_close_directives(rendered)
    assert "close Assets:Pic:Portfolio:US0378331005:USD" in closes
    # The Vanguard position is still open — no close should be emitted.
    assert "GB00B16KPT44" not in closes


def test_cash_only_batch_emits_no_closes() -> None:
    rendered = """\
2020-01-01 open Assets:Pic:Cash:EUR  EUR
2020-01-01 open Expenses:Pic:Fees:Other  EUR

2024-03-01 * "FEE"
  Assets:Pic:Cash:EUR                  -25.00 EUR
  Expenses:Pic:Fees:Other               25.00 EUR
"""
    closes = beancount_writer.render_close_directives(rendered)
    assert closes == ""


def test_close_date_is_day_after_last_sell() -> None:
    """Even when the closing trade happens earlier than other (unrelated)
    transactions in the batch, the close date is the day after the last
    posting *to that specific account*, not the last posting overall."""

    rendered = """\
2020-01-01 open Assets:Pic:Portfolio:US0378331005:USD  US0378331005
2020-01-01 open Assets:Pic:Portfolio:GB00B16KPT44:GBP  GB00B16KPT44

2024-03-01 * "BUY AAPL"
  Assets:Pic:Portfolio:US0378331005:USD  100 US0378331005 {123.45 USD}
  Assets:Pic:Cash:USD                   -12345.00 USD

2024-05-15 * "SELL AAPL FULL"
  Assets:Pic:Portfolio:US0378331005:USD  -100 US0378331005 {} @ 150.00 USD
  Assets:Pic:Cash:USD                    15000.00 USD
  Income:Pic:Pnl:US0378331005           -2655.00 USD

2024-12-31 * "BUY VANGUARD UK"
  Assets:Pic:Portfolio:GB00B16KPT44:GBP  50 GB00B16KPT44 {200.00 GBP}
  Assets:Pic:Cash:GBP                   -10000.00 GBP
"""
    closes = beancount_writer.render_close_directives(rendered)
    # Close date anchors to AAPL's last activity (2024-05-15), not the
    # batch's last activity (2024-12-31).
    assert "2024-05-16 close Assets:Pic:Portfolio:US0378331005:USD" in closes


def test_render_all_appends_close_block() -> None:
    """End-to-end: render_all should now produce open + entries + close."""

    from datetime import date
    from decimal import Decimal
    from pathlib import Path

    from banking_pipeline.models import (
        BankClassification,
        BankId,
        Classification,
        DocumentType,
        ExtractionResult,
        Transaction,
    )

    common_classification_kwargs = {
        "confidence": 0.99,
        "source": "rules",
        "template_id": "pictet.test.v1",
        "bank": BankClassification(bank=BankId.PICTET, confidence=0.99, source="rules"),
    }

    buy = ExtractionResult(
        classification=Classification(
            document_type=DocumentType.BUY_SHARES,
            **common_classification_kwargs,
        ),
        transactions=[
            Transaction(
                trade_date=date(2024, 3, 1),
                settlement_date=date(2024, 3, 3),
                narration="Buy AAPL",
                currency="USD",
                amount=Decimal("-12345.00"),
                isin="US0378331005",
                quantity=Decimal("100"),
                price=Decimal("123.45"),
                account_number="P-12345",
                source_path=Path("buy.pdf"),
            )
        ],
        source_path=Path("buy.pdf"),
    )
    sell = ExtractionResult(
        classification=Classification(
            document_type=DocumentType.SELL_ETF,
            **common_classification_kwargs,
        ),
        transactions=[
            Transaction(
                trade_date=date(2024, 9, 15),
                settlement_date=date(2024, 9, 17),
                narration="Sell AAPL",
                currency="USD",
                amount=Decimal("15000.00"),
                isin="US0378331005",
                # Sells carry a negative quantity (templates extract the
                # signed nominal — see the sell_etf golden's ``-1488``),
                # so this disposal zeroes the +100 from the buy.
                quantity=Decimal("-100"),
                price=Decimal("150.00"),
                account_number="P-12345",
                source_path=Path("sell.pdf"),
            )
        ],
        source_path=Path("sell.pdf"),
    )

    rendered = beancount_writer.render_all([buy, sell])
    # Open at the top, close at the bottom for the AAPL account. The
    # portfolio segment is sanitised (``P-12345`` → ``P12345``) because
    # beancount account components disallow ``-`` / ``.``.
    assert "open Assets:Pic:P12345:US0378331005" in rendered
    assert "close Assets:Pic:P12345:US0378331005" in rendered


def test_render_all_close_zeroed_false_skips_close_block() -> None:
    from datetime import date
    from decimal import Decimal
    from pathlib import Path

    from banking_pipeline.models import (
        BankClassification,
        BankId,
        Classification,
        DocumentType,
        ExtractionResult,
        Transaction,
    )

    common_classification_kwargs = {
        "confidence": 0.99,
        "source": "rules",
        "template_id": "pictet.test.v1",
        "bank": BankClassification(bank=BankId.PICTET, confidence=0.99, source="rules"),
    }

    buy = ExtractionResult(
        classification=Classification(
            document_type=DocumentType.BUY_SHARES,
            **common_classification_kwargs,
        ),
        transactions=[
            Transaction(
                trade_date=date(2024, 3, 1),
                narration="Buy",
                currency="USD",
                amount=Decimal("-100.00"),
                isin="US0378331005",
                quantity=Decimal("1"),
                price=Decimal("100"),
                account_number="P-1",
                source_path=Path("b.pdf"),
            )
        ],
        source_path=Path("b.pdf"),
    )
    sell = ExtractionResult(
        classification=Classification(
            document_type=DocumentType.SELL_ETF,
            **common_classification_kwargs,
        ),
        transactions=[
            Transaction(
                trade_date=date(2024, 9, 1),
                narration="Sell",
                currency="USD",
                amount=Decimal("110.00"),
                isin="US0378331005",
                quantity=Decimal("-1"),
                price=Decimal("110"),
                account_number="P-1",
                source_path=Path("s.pdf"),
            )
        ],
        source_path=Path("s.pdf"),
    )

    rendered = beancount_writer.render_all([buy, sell], close_zeroed=False)
    assert "close" not in rendered
