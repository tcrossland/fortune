"""Duplicate-transaction audit over the JSONL sidecars."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from banking_pipeline import cli, dedup
from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.transaction_sidecar import dump_transactions, transactions_to_jsonl

runner = CliRunner()


def _tx(
    *,
    amount: str = "-1000.00",
    number: str | None = "TX1",
    src: str = "2025/a.pdf",
    isin: str | None = "IE00BKRCQ001",
    narration: str = "buy",
    doctype: DocumentType | None = DocumentType.SUBSCRIPTION_NOTICE,
) -> Transaction:
    return Transaction(
        trade_date=date(2025, 3, 10),
        narration=narration,
        currency="GBP",
        amount=Decimal(amount),
        isin=isin,
        document_type=doctype,
        account_number="K-123456.001",
        transaction_number=number,
        source_path=Path(src),
    )


# --- transaction_key ------------------------------------------------------


def test_key_stable_and_precision_insensitive() -> None:
    # Same event, amount printed at different precision → same key.
    assert transaction_key_of("-1000.00") == transaction_key_of("-1000.0000")


def transaction_key_of(amount: str) -> str:
    return dedup.transaction_key(_tx(amount=amount))


def test_key_changes_with_amount_and_isin() -> None:
    base = dedup.transaction_key(_tx())
    assert dedup.transaction_key(_tx(amount="-1000.01")) != base
    assert dedup.transaction_key(_tx(isin="IE00B3DJ5M15")) != base


def test_key_ignores_reference_and_narration() -> None:
    # The same event from two documents (different ref / narration) keys
    # identically — that's the double-count we want to catch.
    a = dedup.transaction_key(_tx(number="TX1", narration="buy", src="a.pdf"))
    b = dedup.transaction_key(_tx(number="TX9", narration="purchase", src="b.pdf"))
    assert a == b


# --- find_duplicates ------------------------------------------------------


def _member(**kw: object) -> dedup.DuplicateMember:
    sidecar = Path(str(kw.pop("sidecar", "data/2025.transactions.jsonl")))
    return dedup.DuplicateMember(transaction=_tx(**kw), sidecar=sidecar)  # type: ignore[arg-type]


def test_find_duplicates_groups_and_skips_singletons() -> None:
    members = [
        _member(number="TX1", src="a.pdf"),
        _member(number="TX1", src="a.pdf"),  # same event again
        _member(amount="-42.00", number="TX5"),  # unique → not a group
    ]
    groups = dedup.find_duplicates(members)
    assert len(groups) == 1
    assert len(groups[0].members) == 2


def test_group_exact_vs_possible() -> None:
    # Same transaction_number across members → EXACT.
    exact = dedup.find_duplicates(
        [_member(number="TX1"), _member(number="TX1")]
    )
    assert exact[0].exact is True

    # Differing references → only POSSIBLE.
    possible = dedup.find_duplicates(
        [_member(number="TX1"), _member(number="TX2")]
    )
    assert possible[0].exact is False

    # All references missing → not assertable as exact.
    missing = dedup.find_duplicates(
        [_member(number=None), _member(number=None)]
    )
    assert missing[0].exact is False


def test_render_csv_one_row_per_member_with_classification() -> None:
    groups = dedup.find_duplicates([_member(number="TX1"), _member(number="TX1")])
    csv_text = dedup.render_csv(groups)
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("key,classification,")
    assert len(lines) == 3  # header + 2 members
    assert ",exact," in lines[1]


# --- sidecar carries the key ----------------------------------------------


def test_sidecar_stamps_dedup_key() -> None:
    tx = _tx()
    text = transactions_to_jsonl([tx])
    obj = json.loads(text.splitlines()[1])
    assert obj["dedup_key"] == dedup.transaction_key(tx)


# --- CLI ------------------------------------------------------------------


def test_cli_dedup_check_flags_duplicates(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    # Same advice (TX1) ingested into two year files → a duplicate.
    dump_transactions(
        [_tx(number="TX1", src="a.pdf"), _tx(amount="-7.00", number="TX2")],
        data / "2025-K.transactions.jsonl",
    )
    dump_transactions(
        [_tx(number="TX1", src="a.pdf")], data / "2026-K.transactions.jsonl"
    )

    out = tmp_path / "dupes.csv"
    result = runner.invoke(
        cli.app, ["dedup-check", str(data), "--output", str(out)]
    )

    assert result.exit_code == 1, result.output
    assert "EXACT" in result.output
    assert out.exists()
    assert ",exact," in out.read_text(encoding="utf-8")


def test_cli_dedup_check_clean_exits_zero(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    dump_transactions(
        [_tx(number="TX1"), _tx(amount="-7.00", number="TX2")],
        data / "2025-K.transactions.jsonl",
    )
    result = runner.invoke(cli.app, ["dedup-check", str(data)])
    assert result.exit_code == 0, result.output
    assert "No duplicates found" in result.output
