"""Tests for the statement-completeness cross-check.

Phase 1: parse the EUR/USD cash ledger out of a Pictet financial
statement, recovering each movement's sign from the running-balance
delta. Phase 2: diff the parsed lines against the JSONL sidecars
(FX-leg expansion, securities-settlement exclusion, period bounding).

All figures below are **invented** and internally consistent — the parser
self-checks the running balance, so the amounts must reconcile. The
account number uses the allow-listed placeholder ``K-999999.001``.
Following ``test_pictet_balances.py``, the statement text is inline rather
than a fixture file (the discovered ``<lang>/<bank>`` fixtures mask every
digit to ``9``, which can't exercise a balance self-check).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from banking_pipeline.statement_completeness import (
    CashLine,
    MatchStatus,
    StatementParseError,
    diff,
    group_cash_statement,
    lettered_portfolio_map,
    parse_cash_statement_csv,
    parse_current_account,
    parse_statement_period,
    portfolio_is_known,
    resolve_portfolio,
    sidecar_cash_events,
)

# A two-currency current-account statement exercising every shape the
# diff step must later handle: a deposit (credit), a subscription
# (debit), a redemption (credit), a quarterly fee triple, the two legs of
# an internal EUR↔USD transfer, a mid-section page break, and the closing
# balance line.
_STATEMENT = """\
Financial statement in EUR
K-999999.001
IBAN number : LU999999999999999999

Current account statement in EUR
K-999999.001.00.EUR

From 1 January 2099 to 31 December 2099

BOOK. DATE   DESCRIPTION                  VALUE DATE   DEBIT      CREDIT       BALANCE

01.01.2099   Balance carried forward                                          0.00
05.01.2099   Bonificación                 05.01.2099              100'000.00 ^  100'000.00
10.02.2099   Suscripción 100 ACME-FUND    12.02.2099   40'000.00              60'000.00
15.03.2099   Reembolso 50 ACME-FUND       17.03.2099               20'000.00   80'000.00

             Balance carried forward                                          80'000.00
20.06.2099   Gastos de custodia 2° trimestre 2099    30.06.2099   10.00       79'990.00
20.06.2099   Honorarios de administración 2° trimestre 2099  30.06.2099  500.00  79'490.00
20.06.2099   Honorarios de gestión 2° trimestre 2099  30.06.2099  800.00       78'690.00
01.07.2099   Transferencia a su cuenta USD (EUR/USD 1.10000000) 02.07.2099 8'690.00 70'000.00
Balance as at 31 December 2099 in your favour                                 70'000.00
^ Deposits/withdrawals                                            100'000.00
Statement without reversals

Current account statement in USD
K-999999.001.00.USD

01.01.2099   Balance carried forward                                          0.00
03.07.2099   Bonificación                 03.07.2099                5'000.00 ^  5'000.00
04.07.2099   Suscripción 10 USD-FUND      06.07.2099   15'000.00            -10'000.00
05.07.2099   Transferencia de su cuenta ordinario EUR (EUR/USD 1.1)  06.07.2099  10'000.00  0.00
Balance as at 31 December 2099                                                 0.00
Statement without reversals
"""


def _by_desc(lines: list[CashLine], needle: str) -> CashLine:
    matches = [ln for ln in lines if needle in ln.description]
    assert len(matches) == 1, f"{needle!r}: {[ln.description for ln in matches]}"
    return matches[0]


def test_parses_both_currency_sections() -> None:
    lines = parse_current_account(_STATEMENT)
    eur = [ln for ln in lines if ln.currency == "EUR"]
    usd = [ln for ln in lines if ln.currency == "USD"]
    assert len(eur) == 7  # deposit, sub, redemption, 3 fees, transfer-out
    assert len(usd) == 3  # deposit, sub, transfer-in


def test_portfolio_segment_sanitised() -> None:
    lines = parse_current_account(_STATEMENT)
    assert {ln.portfolio for ln in lines} == {"K999999001"}


def test_credit_is_positive_debit_is_negative() -> None:
    lines = parse_current_account(_STATEMENT)
    deposit = next(
        ln for ln in lines if ln.description == "Bonificación" and ln.currency == "EUR"
    )
    assert deposit.amount == Decimal("100000.00")
    sub = _by_desc(lines, "Suscripción 100 ACME")
    assert sub.amount == Decimal("-40000.00")
    redemption = _by_desc(lines, "Reembolso")
    assert redemption.amount == Decimal("20000.00")


def test_dates_and_running_balance_captured() -> None:
    lines = parse_current_account(_STATEMENT)
    sub = _by_desc(lines, "Suscripción 100 ACME")
    assert sub.book_date == "2099-02-10"
    assert sub.value_date == "2099-02-12"
    assert sub.running_balance == Decimal("60000.00")


def test_fee_triple_kept_as_separate_lines() -> None:
    """Pictet books custody / admin / management as three separate
    current-account debits, each its own advice, so the diff matches them
    1:1 — the parser keeps them distinct rather than aggregating."""

    lines = parse_current_account(_STATEMENT)
    fees = [ln for ln in lines if "trimestre" in ln.description]
    assert [ln.amount for ln in fees] == [
        Decimal("-10.00"),
        Decimal("-500.00"),
        Decimal("-800.00"),
    ]


def test_fx_transfer_legs_appear_in_both_sections() -> None:
    lines = parse_current_account(_STATEMENT)
    out_leg = _by_desc(lines, "Transferencia a su cuenta")
    in_leg = _by_desc(lines, "Transferencia de su cuenta")
    assert out_leg.currency == "EUR"
    assert out_leg.amount == Decimal("-8690.00")
    assert in_leg.currency == "USD"
    assert in_leg.amount == Decimal("10000.00")
    # The FX rate in the description must not be parsed as a money token.
    assert out_leg.running_balance == Decimal("70000.00")


def test_page_break_carried_forward_resyncs_balance() -> None:
    """The mid-section ``Balance carried forward`` line re-anchors the
    running balance so the fee rows after it still reconcile."""

    lines = parse_current_account(_STATEMENT)
    first_fee = _by_desc(lines, "Gastos de custodia")
    assert first_fee.amount == Decimal("-10.00")
    assert first_fee.running_balance == Decimal("79990.00")


def test_negative_running_balance_handled() -> None:
    lines = parse_current_account(_STATEMENT)
    usd_sub = _by_desc(lines, "Suscripción 10 USD")
    assert usd_sub.amount == Decimal("-15000.00")
    assert usd_sub.running_balance == Decimal("-10000.00")


def test_spanish_statement_lexicon() -> None:
    """The Madrid statements are fully Spanish: ``Saldo traspasado`` opens
    the section, ``Saldo al`` closes it, ``Extracto de cuenta corriente``
    names the currency. The opening line is date-led here, unlike the
    English ``Balance carried forward``."""

    spanish = """\
Estado financiero
K-999999.001

Extracto de cuenta corriente en EUR
K-999999.001.00.EUR

Del 1 Enero 2099 al 31 Diciembre 2099

FECHA CON.   DESCRIPCIÓN              FECHA VALOR   DÉBITO    CRÉDITO       SALDO

31.12.2098   Saldo traspasado                                              3'860.29
19.01.2099   Bonificación             19.01.2099              50'000.00 ^  53'860.29
23.01.2099   Compra 100 SOME-FUND     25.01.2099   13'860.29               40'000.00
Saldo al 31 Diciembre 2099 a su favor                                      40'000.00
^ Entradas/Salidas                                            50'000.00
Extracto sin anulaciones
"""
    lines = parse_current_account(spanish)
    assert [ln.amount for ln in lines] == [
        Decimal("50000.00"),
        Decimal("-13860.29"),
    ]
    assert lines[0].book_date == "2099-01-19"  # the carried-forward line is skipped


def test_repeated_header_and_numberless_page_break() -> None:
    """pypdfium2 (the pipeline's real loader, unlike ``pdftotext -layout``)
    repeats the section header at every page top and drops the running
    balance from page-break ``Balance carried forward`` lines. The balance
    chain must survive both — tracked per currency, not reset per header."""

    paged = """\
Financial statement in EUR
K-999999.001

Current account statement in EUR
K-999999.001.00.EUR

01.01.2099 Balance carried forward 0.00
05.01.2099 Bonificación 05.01.2099 100'000.00 ^ 100'000.00
10.02.2099 Suscripción 100 ACME 12.02.2099 40'000.00 60'000.00
BIC/SWIFT : PICTLULX IBAN: LU999999999999999999
Current account statement in EUR
K-999999.001.00.EUR
BOOK. DATE DESCRIPTION VALUE DATE DEBIT CREDIT BALANCE
Balance carried forward
15.03.2099 Reembolso 50 ACME 17.03.2099 20'000.00 80'000.00
"""
    lines = parse_current_account(paged)
    # The row after the numberless page break still reconciles.
    redemption = _by_desc(lines, "Reembolso")
    assert redemption.amount == Decimal("20000.00")
    assert redemption.running_balance == Decimal("80000.00")


def test_no_account_number_returns_empty() -> None:
    assert parse_current_account("Current account statement in EUR\n") == []


def test_masked_fixture_degrades_to_empty() -> None:
    """A digit-masked statement (dates ``99.99.9999``) yields no movements
    rather than crashing on an impossible date."""

    masked = (
        "Financial statement in EUR\n"
        "K-999999.999\n"
        "Extracto de cuenta corriente en EUR\n"
        "99.99.9999   Balance carried forward                       9'999.99\n"
        "99.99.9999   Bonificación   99.99.9999   9'999.99   9'999.99\n"
    )
    assert parse_current_account(masked) == []


def test_unreconcilable_balance_raises() -> None:
    """A row whose printed balance is neither prev±amount is a mis-parse
    and must fail loud, not emit a plausible-but-wrong line."""

    broken = (
        "K-999999.001\n"
        "Current account statement in EUR\n"
        "01.01.2099   Balance carried forward                       0.00\n"
        "05.01.2099   Bonificación   05.01.2099   100'000.00   55'555.55\n"
    )
    with pytest.raises(StatementParseError):
        parse_current_account(broken)


# --------------------------------------------------------------------------
# Phase 2: diff against the JSONL sidecars.
# --------------------------------------------------------------------------


def _line(currency: str, date_: str, amount: str, desc: str = "x") -> CashLine:
    return CashLine(
        portfolio="K999999001",
        currency=currency,
        book_date=date_,
        value_date=date_,
        description=desc,
        amount=Decimal(amount),
        running_balance=Decimal("0"),
    )


def test_period_header_parsed_both_locales() -> None:
    assert parse_statement_period("From 22 June 2021 to 31 December 2021") == (
        "2021-06-22",
        "2021-12-31",
    )
    assert parse_statement_period("Del 1 Enero 2023 al 31 Mayo 2023") == (
        "2023-01-01",
        "2023-05-31",
    )
    assert parse_statement_period("no period here") is None


def test_exact_match_consumes_one_event() -> None:
    lines = [_line("EUR", "2021-07-28", "5000.00")]
    rows = [
        {
            "document_type": "pago_interna",
            "currency": "EUR",
            "settlement_date": "2021-07-28",
            "amount": "5000.00",
        }
    ]
    report = diff(lines, rows)
    assert report.matched == 1
    assert not report.has_findings


def test_fx_transfer_counter_leg_expanded() -> None:
    """One sidecar transfer row carries both legs; the statement prints two
    lines (one per currency). Both must match."""

    lines = [
        _line("EUR", "2021-11-11", "-53104.86", "Transferencia a USD"),
        _line("USD", "2021-11-11", "61188.00", "Transferencia de EUR"),
    ]
    rows = [
        {
            "document_type": "transferencia_interna",
            "currency": "EUR",
            "settlement_date": "2021-11-11",
            "amount": "-53104.86",
            "counter_currency": "USD",
            "counter_amount": "61188.00",
        }
    ]
    events = sidecar_cash_events(rows[0])
    assert len(events) == 2
    assert events[1].is_counter_leg
    report = diff(lines, rows)
    assert report.matched == 2
    assert not report.has_findings


def test_securities_settlement_excluded_not_flagged() -> None:
    """Switch / in-specie rows settle outside the cash account, so they
    produce no statement line and must not count as drift."""

    rows = [
        {
            "document_type": "switch_salida",
            "currency": "EUR",
            "settlement_date": "2021-09-30",
            "amount": "60344.79",
        },
        {
            "document_type": "liquidacion_recepcion_de_valores",
            "currency": "EUR",
            "settlement_date": "2021-12-14",
            "amount": "-43690.08",
        },
    ]
    assert sidecar_cash_events(rows[0]) == []
    report = diff([], rows)
    assert report.excluded == 2
    assert not report.has_findings


def test_missing_in_ledger_flagged() -> None:
    """A statement line with no sidecar event is the prime signal."""

    lines = [_line("EUR", "2022-03-15", "-1234.56", "Suscripción phantom")]
    report = diff(lines, [])
    assert [r.status for r in report.missing_in_ledger] == [
        MatchStatus.MISSING_IN_LEDGER
    ]
    assert report.missing_in_ledger[0].description == "Suscripción phantom"


def test_unmatched_in_period_flagged() -> None:
    """A sidecar cash event inside the window with no statement line is a
    finding (possible misdated / spurious booking)."""

    rows = [
        {
            "document_type": "suscripcion",
            "currency": "EUR",
            "settlement_date": "2022-05-10",
            "amount": "-9999.00",
            "narration": "ghost buy",
        }
    ]
    report = diff([], rows, period=("2022-01-01", "2022-12-31"))
    assert len(report.unmatched_in_ledger) == 1
    assert report.out_of_period == 0


def test_out_of_period_event_not_flagged() -> None:
    """A sidecar event settling after the statement's window belongs to the
    next statement, not this one."""

    rows = [
        {
            "document_type": "reembolso",
            "currency": "EUR",
            "settlement_date": "2023-07-05",
            "amount": "37003.74",
        }
    ]
    report = diff([], rows, period=("2023-01-01", "2023-06-30"))
    assert report.unmatched_in_ledger == []
    assert report.out_of_period == 1


def test_window_filter_keeps_counts_per_statement() -> None:
    """``excluded`` / ``out_of_period`` count only rows near the statement's
    window — a far-out row (another year) is skipped entirely, not tallied,
    so the diagnostics don't scale with the whole ``data/`` tree."""

    rows = [
        # In-window securities settlement → counts toward `excluded`.
        {
            "document_type": "switch_salida",
            "currency": "EUR",
            "settlement_date": "2099-06-15",
            "amount": "1000.00",
        },
        # Far-out securities settlement (different year) → not counted.
        {
            "document_type": "switch_salida",
            "currency": "EUR",
            "settlement_date": "2095-06-15",
            "amount": "1000.00",
        },
        # Far-out cash event → skipped by the window, not `out_of_period`.
        {
            "document_type": "suscripcion",
            "currency": "EUR",
            "settlement_date": "2095-06-15",
            "amount": "-500.00",
        },
    ]
    report = diff([], rows, period=("2099-01-01", "2099-12-31"))
    assert report.excluded == 1
    assert report.out_of_period == 0
    assert report.unmatched_in_ledger == []


def test_date_tolerance_matches_value_vs_settlement_drift() -> None:
    line = [_line("EUR", "2021-09-03", "-61909.24")]  # statement BOOK date
    rows = [
        {
            "document_type": "suscripcion",
            "currency": "EUR",
            "settlement_date": "2021-09-08",  # 5 days later
            "amount": "-61909.24",
        }
    ]
    assert diff(line, rows).matched == 1
    assert diff(line, rows, date_tolerance_days=2).matched == 0


def test_duplicate_same_day_amount_each_needs_its_own_event() -> None:
    """Two identical movements consume two distinct sidecar events; a
    single event leaves one line missing."""

    lines = [
        _line("EUR", "2022-02-01", "-5000.00"),
        _line("EUR", "2022-02-01", "-5000.00"),
    ]
    one_event = [
        {
            "document_type": "suscripcion",
            "currency": "EUR",
            "settlement_date": "2022-02-01",
            "amount": "-5000.00",
        }
    ]
    report = diff(lines, one_event)
    assert report.matched == 1
    assert len(report.missing_in_ledger) == 1


# --- Portal CSV cash statement (parse_cash_statement_csv) -------------------
#
# The e-banking export is Windows-1252, semicolon-delimited, dates
# ``YYYY/MM/DD``, ``Net amount`` already signed, ``Account nr.`` *bare* (no
# K-/P- mandate letter). Written cp1252 here so the ``°`` (byte 0xb0) exercises
# the encoding the parser must use. Figures invented, internally consistent so
# the running-balance self-check reconciles. Rows are newest-first, as exported.
_CASH_CSV = (
    "Account nr.;Booking date;Value date;Description of transaction;"
    "Current account currency;Net amount in current account currency;"
    "Balance in current account currency\n"
    "999999.002;2099/07/03;2099/07/03;Bonificación;GBP;5000.00;5000.00\n"
    "999999.001;2099/03/16;2099/03/17;Gastos de custodia 3° trimestre;"
    "EUR;-10.00;59990.00\n"
    "999999.001;2099/02/10;2099/02/12;Suscripción 100 ACME-FUND;"
    "EUR;-40000.00;60000.00\n"
    "999999.001;2099/01/06;2099/01/05;Bonificación;EUR;100000.00;100000.00\n"
)


def _write_cash_csv(tmp_path: Path, text: str = _CASH_CSV) -> Path:
    path = tmp_path / "Cash_statements_by_value_date_20990101000000.csv"
    path.write_bytes(text.encode("cp1252"))
    return path


def test_cash_csv_parses_signed_amounts_and_bare_portfolio(tmp_path: Path) -> None:
    lines = parse_cash_statement_csv(_write_cash_csv(tmp_path))
    assert len(lines) == 4
    # ``Account nr.`` has no mandate letter → portfolio is the bare numeric.
    assert {ln.portfolio for ln in lines} == {"999999001", "999999002"}
    deposit = next(
        ln
        for ln in lines
        if ln.portfolio == "999999001"
        and ln.currency == "EUR"
        and ln.value_date == "2099-01-05"
    )
    assert deposit.amount == Decimal("100000.00")  # credit, already signed
    sub = next(ln for ln in lines if "ACME-FUND" in ln.description)
    assert sub.amount == Decimal("-40000.00")  # debit, already signed
    assert sub.value_date == "2099-02-12" and sub.book_date == "2099-02-10"


def test_cash_csv_balance_self_check_raises_on_break(tmp_path: Path) -> None:
    broken = _CASH_CSV.replace("60000.00", "61000.00")  # breaks EUR chain
    with pytest.raises(StatementParseError):
        parse_cash_statement_csv(_write_cash_csv(tmp_path, broken))


def test_cash_csv_missing_value_date_skipped_in_self_check(tmp_path: Path) -> None:
    # A row with no value date can't be ordered; it's kept but not chained.
    text = _CASH_CSV.replace("2099/07/03;2099/07/03", "2099/07/03;")
    lines = parse_cash_statement_csv(_write_cash_csv(tmp_path, text))
    gbp = next(ln for ln in lines if ln.currency == "GBP")
    assert gbp.value_date is None and gbp.amount == Decimal("5000.00")


def test_group_cash_statement_splits_by_portfolio_and_synthesises_period(
    tmp_path: Path,
) -> None:
    groups = group_cash_statement(parse_cash_statement_csv(_write_cash_csv(tmp_path)))
    by_pf = {pf: (lines, period) for pf, lines, period in groups}
    assert set(by_pf) == {"999999001", "999999002"}
    assert by_pf["999999001"][1] == ("2099-01-05", "2099-03-17")  # min/max value
    assert by_pf["999999002"][1] == ("2099-07-03", "2099-07-03")


def test_lettered_portfolio_map_resolves_bare_csv_account() -> None:
    rows = [
        {"account_number": "K-999999.001"},
        {"account_number": "P-999999.002"},
    ]
    mapping = lettered_portfolio_map(rows)
    assert resolve_portfolio("999999001", mapping) == "K999999001"
    assert resolve_portfolio("999999002", mapping) == "P999999002"
    # An already-lettered portfolio round-trips; an unknown one passes through.
    assert resolve_portfolio("K999999001", mapping) == "K999999001"
    assert resolve_portfolio("999999999", mapping) == "999999999"


def test_portfolio_is_known_flags_mandate_absent_from_sidecars() -> None:
    mapping = lettered_portfolio_map([{"account_number": "K-999999.001"}])
    assert portfolio_is_known("999999001", mapping) is True  # bare CSV account
    assert portfolio_is_known("K999999001", mapping) is True  # lettered
    assert portfolio_is_known("999999002", mapping) is False  # no such sidecar


def test_limit_extension_excluded_from_cash_events() -> None:
    """A limit-extension advice records no cash movement (Net amount 0.00),
    so it yields no sidecar event — not a spurious UNMATCHED."""

    row = {
        "document_type": "limit_extension",
        "currency": "GBP",
        "settlement_date": "2099-02-26",
        "amount": "0.00",
    }
    assert sidecar_cash_events(row) == []
