"""Tests for the Transactions-export reconciliation.

Parse the portal ``Transactions`` CSV into per-leg rows and diff them against
the JSONL sidecars by ``Order nr.``: MISSING (booked but not ingested),
UNMATCHED (ingested but not on the export), AMOUNT_MISMATCH (single-leg
securities figure disagrees). Figures are invented; the account uses the
allow-listed placeholder ``999999.001`` (bare, as the export carries it).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from banking_pipeline.transactions_export import (
    ExportRow,
    MatchStatus,
    group_by_portfolio,
    parse_transactions_csv,
    reconcile,
)

# One leg per line, newest-first. The `ó` (byte 0xf3 in cp1252) exercises the
# encoding. Covers: two single-leg securities orders, a two-leg FX spot sharing
# one order number, and a Forex-forward-open leg (never ingested).
_CSV = (
    "Account nr.;Trade date;Transaction type;Description of transaction;"
    "Current account currency;Net amount in current account currency;Order nr.\n"
    "999999.001;2099/07/03;Forex spot;Débito Forex Spot;USD;-129245.07;1000003\n"
    "999999.001;2099/07/03;Forex spot;Crédito Forex Spot;EUR;112928.78;1000003\n"
    "999999.001;2099/07/02;Forex forward open;FX fwd open;;;1000004\n"
    "999999.001;2099/03/16;Redemption;Redemption 100 ACME-FUND;EUR;20000.00;1000001\n"
    "999999.001;2099/02/10;Subscription;Subscripción 50 ACME-FUND;EUR;-40000.00;1000002\n"
    "999999.002;2099/06/30;Buy;Buy 10 XYZ;GBP;-5000.00;2000001\n"
)


def _write(tmp_path: Path, text: str = _CSV) -> Path:
    path = tmp_path / "Transactions_20990101000000.csv"
    path.write_bytes(text.encode("cp1252"))
    return path


def _sc(
    txn: str,
    *,
    account: str = "K-999999.001",
    amount: str = "0.00",
    currency: str = "EUR",
    trade: str = "2099-03-16",
    doctype: str = "subscription",
) -> dict[str, object]:
    return {
        "transaction_number": txn,
        "account_number": account,
        "amount": amount,
        "currency": currency,
        "trade_date": trade,
        "document_type": doctype,
        "narration": "x",
    }


def _row(
    order: str,
    ttype: str,
    amount: str | None,
    *,
    portfolio: str = "999999001",
    trade: str = "2099-03-16",
) -> ExportRow:
    return ExportRow(
        order_number=order,
        portfolio=portfolio,
        trade_date=trade,
        transaction_type=ttype,
        currency="EUR",
        cash_amount=None if amount is None else Decimal(amount),
        description="d",
    )


def test_parse_fields_and_cp1252(tmp_path: Path) -> None:
    rows = parse_transactions_csv(_write(tmp_path))
    assert len(rows) == 6
    assert {r.portfolio for r in rows} == {"999999001", "999999002"}  # bare
    sub = next(r for r in rows if r.order_number == "1000002")
    assert sub.transaction_type == "Subscription"
    assert sub.cash_amount == Decimal("-40000.00")
    assert sub.trade_date == "2099-02-10"
    assert "Subscripción" in sub.description  # cp1252 decoded
    fx_open = next(r for r in rows if r.transaction_type == "Forex forward open")
    assert fx_open.cash_amount is None


def test_group_by_portfolio_synthesises_trade_window(tmp_path: Path) -> None:
    groups = group_by_portfolio(parse_transactions_csv(_write(tmp_path)))
    by_pf = {pf: period for pf, _rows, period in groups}
    assert by_pf["999999001"] == ("2099-02-10", "2099-07-03")
    assert by_pf["999999002"] == ("2099-06-30", "2099-06-30")


def test_reconcile_clean() -> None:
    export = [
        _row("1000001", "Redemption", "20000.00"),
        _row("1000002", "Subscription", "-40000.00"),
        _row("1000003", "Forex spot", "-129245.07"),
        _row("1000003", "Forex spot", "112928.78"),  # 2-leg FX, one order
        _row("1000004", "Forex forward open", None),  # excluded
    ]
    sidecars = [
        _sc("1000001", amount="20000.00"),
        _sc("1000002", amount="-40000.00"),
        _sc("1000003", amount="-129245.07"),
    ]
    rep = reconcile(export, sidecars, portfolio="K999999001", period=None)
    assert rep.matched == 3
    assert rep.excluded == 1  # the FX-forward-open order
    assert not rep.missing_in_ledger
    assert not rep.unmatched_in_ledger
    assert not rep.amount_mismatches


def test_reconcile_missing_trade() -> None:
    export = [_row("1000001", "Redemption", "20000.00")]
    rep = reconcile(export, [], portfolio="K999999001", period=None)
    assert [f.order_number for f in rep.missing_in_ledger] == ["1000001"]
    assert rep.missing_in_ledger[0].status is MatchStatus.MISSING_IN_LEDGER


def test_reconcile_unmatched_sidecar() -> None:
    sidecars = [_sc("9999999", amount="1.00")]  # ingested, not on the export
    rep = reconcile([], sidecars, portfolio="K999999001", period=None)
    assert [f.order_number for f in rep.unmatched_in_ledger] == ["9999999"]


def test_reconcile_amount_mismatch_on_single_leg_securities() -> None:
    export = [_row("1000002", "Subscription", "-40000.00")]
    sidecars = [_sc("1000002", amount="-39000.00")]  # a cent+ off
    rep = reconcile(export, sidecars, portfolio="K999999001", period=None)
    assert rep.matched == 1
    assert len(rep.amount_mismatches) == 1
    m = rep.amount_mismatches[0]
    assert m.export_amount == Decimal("-40000.00")
    assert m.sidecar_amount == Decimal("-39000.00")


def test_reconcile_amount_not_checked_for_payments() -> None:
    # A Payment isn't in the securities amount-check set — gross-vs-net
    # conventions differ, so a nominal difference must not flag.
    export = [_row("1000005", "Payment", "-12000.00")]
    sidecars = [_sc("1000005", amount="-12043.40")]
    rep = reconcile(export, sidecars, portfolio="K999999001", period=None)
    assert rep.matched == 1
    assert not rep.amount_mismatches


def test_reconcile_multileg_fx_amount_not_checked() -> None:
    export = [
        _row("1000003", "Forex spot", "-129245.07"),
        _row("1000003", "Forex spot", "112928.78"),
    ]
    sidecars = [_sc("1000003", amount="-96912.14")]  # GBP cash leg, differs
    rep = reconcile(export, sidecars, portfolio="K999999001", period=None)
    assert rep.matched == 1
    assert not rep.amount_mismatches  # multi-leg → presence only


def test_reconcile_limit_extension_sidecar_ignored() -> None:
    # A limit-extension advice carries an order number but never appears in the
    # Transactions export — it must not read as UNMATCHED.
    sidecars = [_sc("1168719651", doctype="limit_extension", amount="0.00")]
    rep = reconcile([], sidecars, portfolio="K999999001", period=None)
    assert not rep.unmatched_in_ledger


def test_reconcile_out_of_window_sidecar_not_flagged() -> None:
    # A sidecar trade before the export's window belongs to an earlier export,
    # not a phantom ingest.
    sidecars = [_sc("8888888", trade="2098-01-01", amount="1.00")]
    rep = reconcile(
        [], sidecars, portfolio="K999999001", period=("2099-01-01", "2099-12-31")
    )
    assert not rep.unmatched_in_ledger
    assert rep.out_of_period == 1
