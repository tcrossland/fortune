"""Pictet Spanish-locale monthly statement (``ESTADO_MENSUAL``) extraction.

The Madrid account's statement has a different valuation-page layout from
the English one, so ``balances_extract`` / ``prices_extract`` need locale
handling:

* the account number prints **bare** (``K-NNNNNN.NNN`` on its own line),
  not behind an ``Account no.:`` / ``N° de cuenta:`` label;
* the cash row is ``<CCY> <bal> <name> <bal> <%>`` (currency first), not
  ``<bal> <name> <CCY> <bal>``;
* each holding is **two** lines —
  ``<qty> <desc> <ccy> <cotización> <cost> <valoración> …`` then
  ``ISIN: <isin> <ccy> <gross-unit-cost>`` — and the **cotización** (the
  per-unit quote on the first line), *not* the ISIN-line unit cost, is the
  valuation mark (``qty × cotización ≈ valoración``).

All figures synthetic; ISINs are placeholders.
"""

from __future__ import annotations

from decimal import Decimal

from banking_pipeline.balances_extract import extract_balances_from_statement
from banking_pipeline.prices_extract import extract_prices_from_statement
from banking_pipeline.valuation import raw_from_statement

# Mirrors the real ES "Valoración de la cartera" page: a bare account line,
# a EUR cash row, and two two-line holdings.
_ES = """\
ESTADO FINANCIERO EN EUR (Euro)
K-999999.999
a Valoración de la cartera al 30 Junio 2022 en EUR
CANTIDAD DESCRIPCIÓN COTIZACIÓN COSTE TOTAL NETO EUR VALORACIÓN EUR % DEL TOTAL NO REALIZADO
Liquidez 362'237 7.87
EUR 10'080 Euro 10'080 0.22
1'763.08015 PICTET-ST MONEY MARKET EUR-I EUR 136.42 241'261 240'516 5.22 -745 -0.31
ISIN: LU0000000001 EUR 136.84
10'430 PIMCO GIS-INCOME FD INSTIT.HEDG.EUR EUR 13.42 157'492 139'971 3.04 -17'522 -11.13
ISIN: IE0000000002 EUR 15.10
"""


def test_es_balances() -> None:
    rows = extract_balances_from_statement(_ES)
    by = {r[3]: r for r in rows}
    # Cash: EUR row, asserted one day after the statement's "al" date.
    assert by["EUR"] == (
        "2022-07-01", "Assets:Pic:K999999999:EUR", "10080", "EUR"
    )
    # Securities: quantity from the holding row, account from the bare line.
    assert by["LU0000000001"] == (
        "2022-07-01", "Assets:Pic:K999999999:LU0000000001", "1763.08015",
        "LU0000000001",
    )
    assert by["IE0000000002"][2] == "10430"


def test_es_prices_use_the_cotizacion_not_the_unit_cost() -> None:
    prices = extract_prices_from_statement(_ES, doctype=None, source="es.txt")
    by = {p.commodity: p for p in prices}
    # The cotización (136.42), NOT the ISIN-line gross unit cost (136.84).
    assert (by["LU0000000001"].price, by["LU0000000001"].currency) == (
        "136.42", "EUR"
    )
    assert by["IE0000000002"].price == "13.42"


def test_es_raw_from_statement_values_holdings() -> None:
    raws = raw_from_statement(_ES, "es.txt")
    by = {r.key: r for r in raws}
    sec = by["LU0000000001"]
    assert sec.quantity == Decimal("1763.08015")
    assert sec.price == Decimal("136.42")
    assert sec.currency == "EUR" and not sec.is_cash
    # qty × cotización reconciles with the statement's VALORACIÓN (240'516).
    assert round(sec.quantity * sec.price) == 240519
    assert by["EUR"].is_cash and by["EUR"].quantity == Decimal("10080")
