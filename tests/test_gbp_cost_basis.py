"""GBP-rate posting metadata on security trades (UK CGT groundwork).

The ledger stays in its native trade currency; the trade-date GBP rate
rides along as ``gbp-rate`` posting metadata on the security leg so the
downstream tax-report can do the section-104 / GBP conversion. These
tests pin that metadata's presence, value, and placement — and confirm
that a ``gbp_rate``-free transaction renders byte-identically to before.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from beancount import loader

from banking_pipeline import beancount_writer
from banking_pipeline.models import (
    BankClassification,
    BankId,
    Classification,
    DocumentType,
    Transaction,
)


def _classification(doc_type: DocumentType) -> Classification:
    return Classification(
        document_type=doc_type,
        confidence=0.99,
        source="rules",
        template_id="pictet.test.v1",
        bank=BankClassification(bank=BankId.PICTET, confidence=0.99, source="rules"),
    )


def _buy(*, gbp_rate: Decimal | None, currency: str = "EUR") -> Transaction:
    return Transaction(
        trade_date=date(2024, 3, 1),
        narration="Buy Shares",
        title="Buy Shares",
        currency=currency,
        amount=Decimal("-12345.00"),
        isin="US0378331005",
        quantity=Decimal("100"),
        price=Decimal("123.45"),
        security_currency=currency,
        gbp_rate=gbp_rate,
        account_number="P-12345",
        source_path=Path("buy.pdf"),
    )


def test_buy_emits_gbp_rate_metadata_on_security_posting() -> None:
    rendered = beancount_writer.render_entry(
        _buy(gbp_rate=Decimal("0.8")), _classification(DocumentType.BUY_SHARES)
    )
    lines = rendered.splitlines()
    asset_idx = next(
        i for i, ln in enumerate(lines) if "Assets:Pic:P12345:US0378331005" in ln
    )
    # Metadata sits on the line immediately after the security posting,
    # indented one level deeper so beancount attaches it to that posting.
    assert lines[asset_idx + 1] == '    gbp-rate: "0.8"'


def test_sell_emits_gbp_rate_metadata_on_security_posting() -> None:
    sell = Transaction(
        trade_date=date(2024, 9, 15),
        narration="Sell ETF",
        title="Sell Exchange Traded Fund",
        currency="EUR",
        amount=Decimal("15000.00"),
        isin="US0378331005",
        quantity=Decimal("-100"),
        price=Decimal("150.00"),
        security_currency="EUR",
        gbp_rate=Decimal("0.85"),
        account_number="P-12345",
        source_path=Path("sell.pdf"),
    )
    rendered = beancount_writer.render_entry(sell, _classification(DocumentType.SELL_ETF))
    lines = rendered.splitlines()
    asset_idx = next(
        i for i, ln in enumerate(lines) if "Assets:Pic:P12345:US0378331005" in ln
    )
    assert lines[asset_idx + 1] == '    gbp-rate: "0.85"'


def test_bond_buy_emits_gbp_rate_metadata() -> None:
    bond = Transaction(
        trade_date=date(2023, 11, 24),
        narration="Buy bonds",
        title="Buy bonds",
        currency="EUR",
        amount=Decimal("-89978.20"),
        isin="DE000BU3Z005",
        quantity=Decimal("90000.00"),
        price=Decimal("0.97512"),
        security_currency="EUR",
        fees=Decimal("-408.28"),
        fees_currency="EUR",
        accrued_interest=Decimal("-1809.12"),
        gbp_rate=Decimal("0.8696"),
        account_number="K-123456.001",
        source_path=Path("bond.pdf"),
    )
    rendered = beancount_writer.render_entry(bond, _classification(DocumentType.BUY_BONDS))
    lines = rendered.splitlines()
    asset_idx = next(
        i for i, ln in enumerate(lines) if "Assets:Pic:K123456001:DE000BU3Z005" in ln
    )
    assert lines[asset_idx + 1] == '    gbp-rate: "0.8696"'


def test_no_gbp_rate_renders_without_metadata() -> None:
    rendered = beancount_writer.render_entry(
        _buy(gbp_rate=None), _classification(DocumentType.BUY_SHARES)
    )
    assert "gbp-rate" not in rendered


def test_gbp_currency_trade_emits_no_metadata() -> None:
    # A GBP-denominated trade needs no conversion rate; gbp_rate=1 is
    # the extractor's marker for that and must not produce noise.
    rendered = beancount_writer.render_entry(
        _buy(gbp_rate=Decimal("1"), currency="GBP"),
        _classification(DocumentType.BUY_SHARES),
    )
    assert "gbp-rate" not in rendered


def test_metadata_bearing_entry_balances_under_beancount() -> None:
    # The metadata must not perturb balancing: the entry stays in its
    # native currency and balances exactly as it would without it.
    entry = beancount_writer.render_entry(
        _buy(gbp_rate=Decimal("0.8")), _classification(DocumentType.BUY_SHARES)
    )
    ledger = (
        'option "operating_currency" "GBP"\n'
        'option "inferred_tolerance_default" "EUR:0.005"\n'
        "2020-01-01 open Assets:Pic:P12345:US0378331005\n"
        "2020-01-01 open Assets:Pic:P12345:EUR\n"
        f"{entry}"
    )
    _, errors, _ = loader.load_string(ledger)
    assert not errors, [e.message for e in errors]
