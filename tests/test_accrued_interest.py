"""Accrued-interest leg on bond trades (UK accrued income scheme)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline import beancount_writer
from banking_pipeline.models import (
    BankClassification,
    BankId,
    Classification,
    DocumentType,
    Transaction,
)
from banking_pipeline.writer.format import accrued_interest_account


def _classification(doc_type: DocumentType) -> Classification:
    return Classification(
        document_type=doc_type,
        confidence=0.99,
        source="rules",
        template_id="pictet.test.v1",
        bank=BankClassification(bank=BankId.PICTET, confidence=0.99, source="rules"),
    )


def _bond(*, accrued: Decimal | None, amount: Decimal, doc_type: DocumentType) -> Transaction:
    return Transaction(
        trade_date=date(2023, 11, 24),
        narration="Bond trade",
        title="Buy bonds" if doc_type is DocumentType.BUY_BONDS else "Sell bonds",
        currency="EUR",
        amount=amount,
        isin="DE000BU3Z005",
        quantity=Decimal("90000.00")
        if doc_type is DocumentType.BUY_BONDS
        else Decimal("-90000.00"),
        price=Decimal("0.97512"),
        security_currency="EUR",
        fees=Decimal("-408.28"),
        fees_currency="EUR",
        accrued_interest=accrued,
        account_number="K-123456.001",
        source_path=Path("bond.pdf"),
    )


def test_account_template_default_shape() -> None:
    assert (
        accrued_interest_account("Pic", "K123456001", "DE000BU3Z005")
        == "Income:Pic:K123456001:DE000BU3Z005:Interest"
    )


def test_buy_accrued_interest_is_income_reduction() -> None:
    # Pictet prints the buyer's accrued payment negative; the builder
    # negates it, so the income leg is positive (a debit = reduction of
    # interest income under the accrued income scheme).
    tx = _bond(
        accrued=Decimal("-1809.12"),
        amount=Decimal("-89978.20"),
        doc_type=DocumentType.BUY_BONDS,
    )
    rendered = beancount_writer.render_entry(tx, _classification(DocumentType.BUY_BONDS))
    assert (
        "Income:Pic:K123456001:DE000BU3Z005:Interest       1809.12 EUR ; Accrued interest"
        in rendered
    )


def test_sell_accrued_interest_is_income_recognised() -> None:
    tx = _bond(
        accrued=Decimal("1945.23"),
        amount=Decimal("94128.80"),
        doc_type=DocumentType.SELL_BONDS,
    )
    rendered = beancount_writer.render_entry(tx, _classification(DocumentType.SELL_BONDS))
    assert (
        "Income:Pic:K123456001:DE000BU3Z005:Interest      -1945.23 EUR ; Accrued interest"
        in rendered
    )


def test_no_accrued_interest_omits_leg() -> None:
    tx = _bond(
        accrued=None,
        amount=Decimal("-87760.80"),
        doc_type=DocumentType.BUY_BONDS,
    )
    rendered = beancount_writer.render_entry(tx, _classification(DocumentType.BUY_BONDS))
    assert "Accrued interest" not in rendered
    assert ":Interest" not in rendered
