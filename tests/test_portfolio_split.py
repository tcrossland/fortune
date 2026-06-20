"""Per-account ledger split (``portfolio-split`` / ``generate_per_account``).

Each per-year ingest file is one bank+account stream, so the split groups
files by owning account and writes one independently-loadable ledger per
account — own options, opens, closes, and ``../`` includes of that
account's per-year files plus prices (never balances).
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from banking_pipeline import cli, portfolio_aggregate

runner = CliRunner()


# --- account-key derivation ----------------------------------------------


def test_account_key_pictet_includes_portfolio_segment() -> None:
    # Single-segment prefix (``Pic``) → prefix + the portfolio segment.
    assert portfolio_aggregate._account_key("Assets:Pic:K123456001:EUR") == (
        "Pic:K123456001"
    )
    assert portfolio_aggregate._account_key(
        "Income:Pic:P123456002:Other"
    ) == "Pic:P123456002"


def test_account_key_vanguard_is_the_two_segment_prefix() -> None:
    # Multi-segment prefix (``Vgd:ISA``) is itself the account key.
    assert portfolio_aggregate._account_key("Assets:Vgd:ISA:VMIG") == "Vgd:ISA"
    assert portfolio_aggregate._account_key("Expenses:Vgd:ISA:Fees") == "Vgd:ISA"


def test_account_key_counterparty_is_none() -> None:
    # No recognised bank prefix → no group of its own.
    assert (
        portfolio_aggregate._account_key("Equity:Transfers:Revolut:GBP") is None
    )
    assert portfolio_aggregate._account_key("Equity:Property:Bristol") is None


# --- fixture project ------------------------------------------------------


def _data_dir(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    # A Pictet K transaction with a Revolut *counterparty* leg — the
    # minority key must not split the file off into a Revolut group.
    (data / "2025-K.beancount").write_text(
        '2025-01-02 * "Payment" "x"\n'
        "  Equity:Transfers:Revolut:GBP      100.00 GBP\n"
        "  Assets:Pic:K123456001:GBP        -100.00 GBP\n"
        "  Expenses:Pic:K123456001:Other\n",
        encoding="utf-8",
    )
    (data / "2025-P.beancount").write_text(
        '2025-03-04 * "Pago" "y"\n'
        "  Assets:Pic:P123456002:EUR        -50.00 EUR\n"
        "  Expenses:Pic:P123456002:Other\n",
        encoding="utf-8",
    )
    (data / "vanguard-isa.beancount").write_text(
        '2025-04-05 * "Deposit" "z"\n'
        "  Assets:Vgd:ISA:GBP                200.00 GBP\n"
        "  Equity:Vgd:ISA:Contributions\n",
        encoding="utf-8",
    )
    (data / "prices.beancount").write_text(
        "2025-01-31 price GBP 1.0 GBP\n", encoding="utf-8"
    )
    (data / "balances.beancount").write_text(
        "2025-01-31 balance Assets:Pic:K123456001:GBP 0.00 GBP\n",
        encoding="utf-8",
    )
    return data


# --- grouping -------------------------------------------------------------


def test_group_files_by_account(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    files = sorted(data.glob("20*.beancount")) + [data / "vanguard-isa.beancount"]
    groups = portfolio_aggregate.group_files_by_account(files)

    assert set(groups) == {"Pic:K123456001", "Pic:P123456002", "Vgd:ISA"}
    # The 2025-K file (with its Revolut leg) belongs to the K account only.
    assert [p.name for p in groups["Pic:K123456001"]] == ["2025-K.beancount"]


# --- generate -------------------------------------------------------------


def test_generate_per_account_writes_independent_ledgers(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    written = portfolio_aggregate.generate_per_account(data)

    names = {p.name for p, _, _ in written}
    assert names == {
        "Pic-K123456001.beancount",
        "Pic-P123456002.beancount",
        "Vgd-ISA.beancount",
    }
    assert all(p.parent == data / "accounts" for p, _, _ in written)

    k_text = (data / "accounts" / "Pic-K123456001.beancount").read_text(
        encoding="utf-8"
    )
    # Own options so it loads standalone.
    assert 'option "operating_currency" "GBP"' in k_text
    assert 'option "booking_method" "FIFO"' in k_text
    # Includes its own per-year file + shared prices, one level up.
    assert 'include "../2025-K.beancount"' in k_text
    assert 'include "../prices.beancount"' in k_text
    # Balances are NOT included (assertions span every account).
    assert "balances.beancount" not in k_text
    # Only this account's per-year file — not P's or the ISA's.
    assert "2025-P.beancount" not in k_text
    assert "vanguard-isa.beancount" not in k_text
    # The counterparty account is centrally opened (so the file balances).
    assert "open Equity:Transfers:Revolut:GBP" in k_text


def test_generate_per_account_threads_extra_options(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    opt = 'option "inferred_tolerance_default" "GBP:0.005"'
    written = portfolio_aggregate.generate_per_account(
        data, extra_options=[opt]
    )
    for path, _, _ in written:
        assert opt in path.read_text(encoding="utf-8")


def test_inferred_tolerance_options_extraction(tmp_path: Path) -> None:
    ledger = tmp_path / "main.beancount"
    ledger.write_text(
        'option "title" "x"\n'
        'option "inferred_tolerance_default" "GBP:0.005"\n'
        'option "inferred_tolerance_default" "JPY:0.5"\n'
        'option "operating_currency" "GBP"\n',
        encoding="utf-8",
    )
    opts = portfolio_aggregate.inferred_tolerance_options(ledger)
    assert opts == [
        'option "inferred_tolerance_default" "GBP:0.005"',
        'option "inferred_tolerance_default" "JPY:0.5"',
    ]
    # Missing file → no options, no error.
    assert portfolio_aggregate.inferred_tolerance_options(tmp_path / "nope") == []


# --- CLI ------------------------------------------------------------------


def test_portfolio_split_cli(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    (tmp_path / "main.beancount").write_text(
        'option "inferred_tolerance_default" "EUR:0.005"\n', encoding="utf-8"
    )
    result = runner.invoke(
        cli.app,
        [
            "portfolio-split",
            str(data),
            "--root-ledger",
            str(tmp_path / "main.beancount"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Pic:K123456001" in result.output
    p_text = (data / "accounts" / "Pic-P123456002.beancount").read_text(
        encoding="utf-8"
    )
    assert 'option "inferred_tolerance_default" "EUR:0.005"' in p_text
