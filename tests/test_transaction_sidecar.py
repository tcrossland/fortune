"""JSONL transaction sidecar: round-trip, precision, CLI, integration."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from banking_pipeline import cli
from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.transaction_sidecar import (
    SCHEMA,
    dump_transactions,
    load_transactions,
    sidecar_path,
    transactions_to_jsonl,
)


def _full_tx() -> Transaction:
    """A transaction exercising every UK-tax-relevant field."""
    return Transaction(
        trade_date=date(2026, 5, 1),
        settlement_date=date(2026, 5, 3),
        narration="Dividend - APPLE INC",
        title="Dividend",
        currency="USD",
        amount=Decimal("85.0000"),
        isin="US0378331005",
        quantity=Decimal("100.000"),
        price=Decimal("1.00"),
        gbp_rate=Decimal("0.79123456"),
        gross_income=Decimal("100.0000"),
        withholding_tax=Decimal("15.0000"),
        withholding_country="US",
        accrued_interest=Decimal("-12.34"),
        account_number="P-999999.999",
        source_path=Path("inbox/apple_div.pdf"),
    )


def test_round_trip_preserves_all_fields(tmp_path: Path) -> None:
    tx = _full_tx()
    path = tmp_path / "out.transactions.jsonl"
    dump_transactions([tx], path, source_document="inbox/apple_div.pdf")
    loaded = load_transactions(path)
    assert len(loaded) == 1
    assert loaded[0] == tx


def test_header_line_carries_schema_and_source(tmp_path: Path) -> None:
    path = tmp_path / "out.transactions.jsonl"
    dump_transactions([_full_tx()], path, source_document="inbox/apple_div.pdf")
    first = path.read_text(encoding="utf-8").splitlines()[0]
    header = json.loads(first)
    assert header == {"_schema": SCHEMA, "source_document": "inbox/apple_div.pdf"}


def test_decimal_serialises_as_string_not_float() -> None:
    text = transactions_to_jsonl([_full_tx()])
    # The transaction line (second line) must encode decimals as JSON
    # strings — a float would lose precision on large/odd values.
    tx_obj = json.loads(text.splitlines()[1])
    assert tx_obj["amount"] == "85.0000"
    assert tx_obj["gbp_rate"] == "0.79123456"
    assert isinstance(tx_obj["amount"], str)
    assert isinstance(tx_obj["gbp_rate"], str)


def test_decimal_precision_preserved_through_round_trip(tmp_path: Path) -> None:
    tx = _full_tx()
    path = tmp_path / "out.transactions.jsonl"
    dump_transactions([tx], path)
    loaded = load_transactions(path)[0]
    assert loaded.amount == Decimal("85.0000")
    assert loaded.gbp_rate == Decimal("0.79123456")
    assert loaded.accrued_interest == Decimal("-12.34")


def test_empty_results_write_header_only(tmp_path: Path) -> None:
    path = tmp_path / "empty.transactions.jsonl"
    dump_transactions([], path, source_document="inbox/statement.pdf")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1  # header only
    assert load_transactions(path) == []


def test_sidecar_path_replaces_suffix() -> None:
    assert sidecar_path(Path("data/2024-K.beancount")) == Path(
        "data/2024-K.transactions.jsonl"
    )


# --- CLI integration -------------------------------------------------------

_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "en"
    / "pictet"
    / "dividend_notice.us_wht.txt"
)


@pytest.fixture
def txt_as_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the ingest path read ``.txt`` fixtures as document text.

    The CLI ingests via ``load_pdf`` (pypdfium2), which can't open the
    plain-text fixtures; patch the symbol the pipeline imports so the
    fixture text flows through classification + extraction unchanged.
    """

    def _load(path: Path) -> RawDocument:
        return RawDocument(
            path=path, text=path.read_text(encoding="utf-8"), page_count=1
        )

    monkeypatch.setattr("banking_pipeline.pipeline.load_pdf", _load)


def test_ingest_writes_sidecar_next_to_beancount(
    tmp_path: Path, txt_as_pdf: None
) -> None:
    out = tmp_path / "out.beancount"
    runner = CliRunner()
    result = runner.invoke(cli.app, ["ingest", str(_FIXTURE), "--output", str(out)])
    assert result.exit_code == 0, result.output

    sidecar = tmp_path / "out.transactions.jsonl"
    assert out.exists()
    assert sidecar.exists()
    loaded = load_transactions(sidecar)
    assert len(loaded) == 1
    assert loaded[0].isin == "US0378331005"
    assert loaded[0].withholding_tax == Decimal("15.00")


def test_dump_transactions_cli_prints_jsonl_to_stdout(
    tmp_path: Path, txt_as_pdf: None
) -> None:
    runner = CliRunner()
    result = runner.invoke(cli.app, ["dump-transactions", str(_FIXTURE)])
    assert result.exit_code == 0, result.output

    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    header = json.loads(lines[0])
    assert header["_schema"] == SCHEMA
    assert header["source_document"] == str(_FIXTURE)
    tx_obj = json.loads(lines[1])
    assert tx_obj["isin"] == "US0378331005"
    # No on-disk ledger written.
    assert not (tmp_path / "out.beancount").exists()
