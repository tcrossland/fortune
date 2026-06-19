"""Unit tests for the pure switch-leg pairing matcher.

Covers the order-date primary path (1:1, FX where amounts don't net,
1:many split), the amount-netting fallback (legs with no order date,
within cent tolerance), and the conservative non-matches (orphan, two
ambiguous same-day switches) — plus determinism under input reordering.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.switch_pairing import pair_switches

_ACCOUNT = "K-123456.001"


def _leg(
    *,
    doctype: DocumentType,
    number: str,
    amount: str,
    currency: str = "EUR",
    isin: str = "LU0000000000",
    booking: date = date(2023, 8, 1),
    order: date | None = date(2023, 7, 27),
    account: str = _ACCOUNT,
    security_currency: str | None = None,
) -> Transaction:
    return Transaction(
        trade_date=booking,
        booking_date=booking,
        order_date=order,
        narration="switch leg",
        currency=currency,
        amount=Decimal(amount),
        isin=isin,
        security_currency=security_currency,
        account_number=account,
        transaction_number=number,
        document_type=doctype,
        source_path=Path("inbox/switch.pdf"),
    )


def _salida(**kw: object) -> Transaction:
    return _leg(doctype=DocumentType.SWITCH_SALIDA, **kw)  # type: ignore[arg-type]


def _entrada(**kw: object) -> Transaction:
    return _leg(doctype=DocumentType.SWITCH_ENTRADA, **kw)  # type: ignore[arg-type]


def test_one_to_one_same_currency_pairs_on_salida_number() -> None:
    salida = _salida(number="889193120", amount="44794.75", isin="LU111")
    entrada = _entrada(number="889193126", amount="-44794.75", isin="LU222")

    result = pair_switches([salida, entrada])

    assert result.assignments == {
        "889193120": "889193120",
        "889193126": "889193120",
    }
    assert result.unpaired == []
    assert result.in_batch_orphans == []


def test_fx_pair_that_does_not_net_still_pairs_on_order_date() -> None:
    # The entrada's EUR clearing amount is an independent USD->EUR
    # conversion, so the two legs are 0.33 apart — amount-netting alone
    # could never confirm the pair, but the shared order date does.
    salida = _salida(number="100", amount="60344.79", isin="LU0955011761")
    entrada = _entrada(
        number="111",
        amount="-60345.12",
        isin="LU1528092635",
        security_currency="USD",
    )

    result = pair_switches([salida, entrada])

    assert result.assignments == {"100": "100", "111": "100"}
    assert result.in_batch_orphans == []


def test_one_salida_funds_two_entradas_under_shared_order_date() -> None:
    salida = _salida(number="200", amount="100000.00", isin="LU0")
    e1 = _entrada(number="201", amount="-60000.00", isin="LU1")
    e2 = _entrada(number="202", amount="-40000.00", isin="LU2")

    result = pair_switches([salida, e1, e2])

    assert result.assignments == {"200": "200", "201": "200", "202": "200"}
    assert result.unpaired == []


def test_cent_rounding_pairs_via_netting_when_no_order_date() -> None:
    # No order date forces the amount-netting fallback; a one-cent
    # residual is within the per-leg tolerance.
    salida = _salida(number="300", amount="5000.00", isin="LU1", order=None)
    entrada = _entrada(number="301", amount="-5000.01", isin="LU2", order=None)

    result = pair_switches([salida, entrada])

    assert result.assignments == {"300": "300", "301": "300"}
    assert result.in_batch_orphans == []


def test_lone_salida_is_unpaired_not_orphan() -> None:
    salida = _salida(number="400", amount="1234.00")

    result = pair_switches([salida])

    assert result.assignments == {}
    assert [t.transaction_number for t in result.unpaired] == ["400"]
    assert result.in_batch_orphans == []


def test_two_ambiguous_same_day_switches_left_unpaired() -> None:
    # Two salidas and two entradas, identical amounts, same order date:
    # the matcher can't tell which sell funds which buy, so it leaves all
    # four unpaired rather than mis-link. They net in aggregate, so it's a
    # benign ambiguity (warning), not a strict orphan.
    s1 = _salida(number="500", amount="10000.00", isin="LU1")
    s2 = _salida(number="501", amount="10000.00", isin="LU2")
    e1 = _entrada(number="502", amount="-10000.00", isin="LU3")
    e2 = _entrada(number="503", amount="-10000.00", isin="LU4")

    result = pair_switches([s1, s2, e1, e2])

    assert result.assignments == {}
    assert {t.transaction_number for t in result.unpaired} == {
        "500",
        "501",
        "502",
        "503",
    }
    assert result.in_batch_orphans == []


def test_non_netting_same_order_date_pair_is_an_orphan() -> None:
    # No order date (so Phase 1 can't pair) and amounts that don't net:
    # opposite sides in one bucket that should be a switch but don't add
    # up — a likely extraction bug, flagged for --strict.
    salida = _salida(number="600", amount="100.00", order=None)
    entrada = _entrada(number="601", amount="-97.00", order=None)

    result = pair_switches([salida, entrada])

    assert result.assignments == {}
    assert {t.transaction_number for t in result.in_batch_orphans} == {
        "600",
        "601",
    }


def test_different_accounts_do_not_cross_pair() -> None:
    salida = _salida(number="700", amount="500.00", account="K-123456.001")
    entrada = _entrada(number="701", amount="-500.00", account="P-999999.999")

    result = pair_switches([salida, entrada])

    assert result.assignments == {}
    assert result.in_batch_orphans == []  # different buckets, not orphans


def test_non_switch_transactions_are_ignored() -> None:
    salida = _salida(number="800", amount="500.00")
    entrada = _entrada(number="801", amount="-500.00")
    dividend = Transaction(
        trade_date=date(2023, 8, 1),
        narration="Dividend",
        currency="EUR",
        amount=Decimal("12.34"),
        transaction_number="802",
        document_type=DocumentType.DIVIDEND_NOTICE,
        source_path=Path("inbox/div.pdf"),
    )

    result = pair_switches([salida, dividend, entrada])

    assert "802" not in result.assignments
    assert result.assignments == {"800": "800", "801": "800"}


def test_deterministic_under_input_reordering() -> None:
    salida = _salida(number="900", amount="44794.75", isin="LU1")
    entrada = _entrada(number="901", amount="-44794.75", isin="LU2")

    forward = pair_switches([salida, entrada])
    reversed_ = pair_switches([entrada, salida])

    assert forward.assignments == reversed_.assignments
    assert [t.transaction_number for t in forward.unpaired] == [
        t.transaction_number for t in reversed_.unpaired
    ]
