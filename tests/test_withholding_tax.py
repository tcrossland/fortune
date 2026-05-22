"""Foreign withholding-tax split on dividend income (SA106)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from beancount import loader
from pydantic import ValidationError

from banking_pipeline import beancount_writer
from banking_pipeline.fields import HybridExtractor
from banking_pipeline.models import (
    BankClassification,
    BankId,
    Classification,
    DocumentType,
    ExtractionResult,
    RawDocument,
    Transaction,
)


def _dividend(**overrides: object) -> Transaction:
    base: dict[str, object] = dict(
        trade_date=date(2026, 5, 1),
        narration="Dividend - APPLE INC",
        title="Dividend",
        currency="USD",
        amount=Decimal("85.00"),
        isin="US0378331005",
        gross_income=Decimal("100.00"),
        withholding_tax=Decimal("15.00"),
        withholding_country="US",
        account_number="P-999999.999",
        source_path=Path("d.pdf"),
    )
    base.update(overrides)
    return Transaction(**base)  # type: ignore[arg-type]


# --- model validator -------------------------------------------------------


def test_valid_wht_transaction_constructs() -> None:
    tx = _dividend()
    assert tx.withholding_tax == Decimal("15.00")


def test_wht_exceeding_gross_is_rejected() -> None:
    with pytest.raises(ValidationError, match="exceeds gross_income"):
        _dividend(
            withholding_tax=Decimal("150.00"),
            gross_income=Decimal("100.00"),
            amount=Decimal("-50.00"),
        )


def test_wht_arithmetic_mismatch_is_rejected() -> None:
    # gross - wht = 85, but net amount claims 90.
    with pytest.raises(ValidationError, match="does not\\s+equal net amount"):
        _dividend(amount=Decimal("90.00"))


def test_wht_without_country_is_rejected() -> None:
    with pytest.raises(ValidationError, match="withholding_country is None"):
        _dividend(withholding_country=None)


def test_wht_without_gross_income_is_rejected() -> None:
    with pytest.raises(ValidationError, match="gross_income is None"):
        _dividend(gross_income=None, amount=Decimal("85.00"))


def test_no_wht_transaction_is_unconstrained() -> None:
    # A plain net dividend (no WHT fields) constructs freely.
    tx = Transaction(
        trade_date=date(2026, 5, 1),
        narration="Dividend",
        currency="GBP",
        amount=Decimal("1242.50"),
        isin="LU2096759431",
        source_path=Path("d.pdf"),
    )
    assert tx.withholding_tax is None


# --- builder ---------------------------------------------------------------


def _classification() -> Classification:
    return Classification(
        document_type=DocumentType.DIVIDEND_NOTICE,
        confidence=0.99,
        source="rules",
        template_id="pictet.dividend_notice.v1",
        bank=BankClassification(bank=BankId.PICTET, confidence=0.99, source="rules"),
    )


def test_builder_emits_three_leg_wht_split() -> None:
    rendered = beancount_writer.render_entry(_dividend(), _classification())
    assert "Income:Pic:P999999999:US0378331005:Dividend       -100.00 USD" in rendered
    assert "Expenses:Tax:Withholding:US                         15.00 USD" in rendered
    assert "Assets:Pic:P999999999:USD                           85.00 USD" in rendered


def test_builder_without_wht_is_two_leg() -> None:
    tx = Transaction(
        trade_date=date(2026, 5, 1),
        narration="Dividend",
        title="Dividend",
        currency="GBP",
        amount=Decimal("1242.50"),
        isin="LU2096759431",
        account_number="P-999999.999",
        source_path=Path("d.pdf"),
    )
    rendered = beancount_writer.render_entry(tx, _classification())
    assert "Withholding" not in rendered
    assert "Income:Pic:P999999999:LU2096759431:Dividend      -1242.50 GBP" in rendered


# --- open directives + bean-check ------------------------------------------


def test_open_directives_include_withholding_account() -> None:
    result = ExtractionResult(
        classification=_classification(),
        transactions=[_dividend()],
        source_path=Path("d.pdf"),
    )
    opens = beancount_writer.render_open_directives([result])
    assert "open Expenses:Tax:Withholding:US" in opens


# --- withholding_country domicile fallback ---------------------------------

_WHT_DIVIDEND_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "en"
    / "pictet"
    / "dividend_notice.us_wht.txt"
)


def _extract_wht_dividend(extractor: HybridExtractor) -> Transaction:
    doc = RawDocument(
        path=_WHT_DIVIDEND_FIXTURE,
        text=_WHT_DIVIDEND_FIXTURE.read_text(encoding="utf-8"),
        page_count=1,
    )
    txns, _ = extractor.extract(doc, _classification_for(DocumentType.DIVIDEND_NOTICE))
    return txns[0]


def _classification_for(doc_type: DocumentType) -> Classification:
    return Classification(
        document_type=doc_type,
        confidence=0.95,
        source="rules",
        template_id="pictet.dividend_notice.v1",
        bank=BankClassification(bank=BankId.PICTET, confidence=0.99, source="rules"),
    )


def test_country_defaults_to_isin_prefix_without_metadata() -> None:
    tx = _extract_wht_dividend(HybridExtractor(commodity_domiciles={}))
    assert tx.withholding_country == "US"  # ISIN US0378331005 prefix


def test_commodity_domicile_overrides_isin_prefix() -> None:
    # The fixture's ISIN is US-prefixed, but the curated domicile says
    # Jersey — the authoritative metadata must win.
    tx = _extract_wht_dividend(
        HybridExtractor(commodity_domiciles={"US0378331005": "JE"})
    )
    assert tx.withholding_country == "JE"


def test_domicile_override_only_for_matching_isin() -> None:
    tx = _extract_wht_dividend(
        HybridExtractor(commodity_domiciles={"IE00B3VWN518": "IE"})
    )
    assert tx.withholding_country == "US"  # different ISIN → no override


def test_wht_entry_balances_with_opens() -> None:
    result = ExtractionResult(
        classification=_classification(),
        transactions=[_dividend()],
        source_path=Path("d.pdf"),
    )
    opens = beancount_writer.render_open_directives([result])
    entry = beancount_writer.render_entry(_dividend(), _classification())
    ledger = (
        'option "operating_currency" "GBP"\n'
        'option "inferred_tolerance_default" "USD:0.005"\n'
        "2020-01-01 open Assets:Pic:P999999999:USD\n"
        f"{opens}\n"
        f"{entry}"
    )
    _, errors, _ = loader.load_string(ledger)
    assert not errors, [e.message for e in errors]
