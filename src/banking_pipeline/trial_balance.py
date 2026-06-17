"""Trial-balance report.

Lists every account's closing balance from the beancount ledger (via
``bean-query``, since the cost-basis ``Realized`` / ``Unrealized`` legs are
computed at load time and can't be summed from the sidecars). Securities
show in units, cash in its native currency.

Because units of a fund and a foreign-cash balance aren't legible as
wealth, the **Assets** and **Liabilities** sections also carry a GBP
market-value column: each account's ``value()`` (qty × latest mark, in the
quote currency) converted to GBP via the same :class:`GbpRateSource` the
``concentration`` report uses. A balance with no mark, or a currency with
no GBP rate, is left blank in the GBP column and surfaced as a warning —
never silently converted. Equity / Income / Expenses stay native: they're
cumulative flows whose spot-rate conversion would be meaningless (and a
single-currency total would need an ``Equity:Conversions`` plug).

**This does not reconcile with the statement-valuation reports**
(``concentration`` / ``net-worth`` / ``allocation`` /
``portfolio-allocation``), by design — they answer "what is it worth?"
differently and will not tie out:

* *source* — here, the **ledger's current positions** (what you actually
  hold per ``bean-query``); there, the **latest statement snapshot** per
  portfolio. A position traded after the last statement shows here but not
  there, and vice-versa.
* *as-of* — here, **today** (``value()`` at the latest price-db mark);
  there, each portfolio's **last statement date**.
* *scope* — here, only what the loaded ledger contains (property only if
  ``main.beancount`` ``include``s ``property.beancount``); there, off-ledger
  property is folded in from ``property.toml``.

Use the statement reports for the GBP wealth view; use this for a
ledger-faithful account-by-account check.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from banking_pipeline.bean_query import QueryResult, run_query
from banking_pipeline.fx.gbp_rates import GbpRateSource
from banking_pipeline.report_format import gbp, money, rate_gap_lines
from banking_pipeline.tax.uk.currency import RateGap, to_gbp

# Account roots, in trial-balance presentation order.
ACCOUNT_TYPES: tuple[str, ...] = (
    "Assets", "Liabilities", "Equity", "Income", "Expenses",
)
# Only these get a GBP market-value column (see the module docstring).
VALUED_TYPES: frozenset[str] = frozenset({"Assets", "Liabilities"})

# One bean-query row per account: its unit balance (for display) and its
# market value (for the GBP conversion). ``value()`` marks securities to
# the latest price in the quote currency and leaves cash as-is.
_BQL = (
    "SELECT account, units(sum(position)) AS units, "
    "value(sum(position)) AS market GROUP BY account ORDER BY account"
)


@dataclass(frozen=True)
class TrialBalanceLine:
    account: str
    type: str
    # (amount, commodity) pairs — usually one (a cash currency or an ISIN);
    # multiple only if an account ever holds more than one commodity.
    native: tuple[tuple[Decimal, str], ...]
    # GBP market value for Assets / Liabilities; None otherwise, or when the
    # account couldn't be valued (flagged in the report's gaps).
    value_gbp: Decimal | None


@dataclass(frozen=True)
class TrialBalance:
    as_of: date
    lines: tuple[TrialBalanceLine, ...]
    # GBP market value of all valued Assets + Liabilities (the Lombard loan,
    # a negative Assets:…:GBP balance, nets in here).
    assets_gbp: Decimal
    # Asset/Liability accounts whose value() left them in a non-currency
    # commodity (no mark in the price db) — excluded from the GBP total.
    missing_prices: tuple[str, ...]
    # Foreign balances with no GBP rate (excluded from the GBP total).
    rate_gaps: tuple[RateGap, ...]


def _is_currency(token: str) -> bool:
    return len(token) == 3 and token.isalpha()


def parse_amounts(field: str) -> list[tuple[Decimal, str]]:
    """Parse a bean-query inventory cell into ``(amount, commodity)`` pairs.

    bean-query's CSV emits plain numbers (no thousands separators) and
    comma-joins a multi-commodity inventory, so a comma is purely the
    position delimiter. The empty cell (a closed / zero account) yields
    ``[]``; zero amounts and unparseable tokens are dropped.
    """

    out: list[tuple[Decimal, str]] = []
    for holding in field.split(","):
        parts = holding.split()
        if len(parts) != 2:
            continue
        amount_str, commodity = parts
        try:
            amount = Decimal(amount_str)
        except InvalidOperation:
            continue
        if amount != 0:
            out.append((amount, commodity))
    return out


def _value_account_gbp(
    market: list[tuple[Decimal, str]],
    *,
    on_date: date,
    rate_source: GbpRateSource,
) -> tuple[Decimal | None, RateGap | None, str | None]:
    """GBP value of one account's market position.

    Returns ``(value_gbp, rate_gap, missing_commodity)``: a converted total,
    or — if any leg can't be valued — ``None`` plus the reason (a non-GBP
    currency with no rate → ``rate_gap``; a non-currency commodity, i.e. no
    mark → ``missing_commodity``). Reasons are mutually exclusive per call:
    the first failing leg wins, so the whole account is flagged once.
    """

    total = Decimal(0)
    for amount, commodity in market:
        if not _is_currency(commodity):
            return None, None, commodity  # value() found no mark
        value = to_gbp(
            amount, currency=commodity, on_date=on_date, source=rate_source
        )
        if value is None:
            return None, RateGap.at(commodity, commodity, on_date), None
        total += value
    return total, None, None


def build_trial_balance(
    result: QueryResult,
    *,
    on_date: date,
    rate_source: GbpRateSource,
) -> TrialBalance:
    """Build the trial balance from a ``bean-query`` result.

    Each row is ``(account, units, market)``. Closed / zero accounts (empty
    unit cell) are dropped. Assets / Liabilities get a GBP column from their
    ``market`` value; the GBP total nets them. Other types stay native.
    """

    lines: list[TrialBalanceLine] = []
    missing: list[str] = []
    gaps: list[RateGap] = []
    assets_gbp = Decimal(0)

    for row in result.rows:
        if len(row) < 3:
            continue
        account, units_field, market_field = row[0].strip(), row[1], row[2]
        native = parse_amounts(units_field)
        if not native:
            continue
        acct_type = account.split(":", 1)[0]

        value_gbp: Decimal | None = None
        if acct_type in VALUED_TYPES:
            value_gbp, gap, miss = _value_account_gbp(
                parse_amounts(market_field), on_date=on_date,
                rate_source=rate_source,
            )
            if value_gbp is not None:
                assets_gbp += value_gbp
            elif gap is not None:
                gaps.append(gap)
            elif miss is not None:
                missing.append(account)
        lines.append(
            TrialBalanceLine(account, acct_type, tuple(native), value_gbp)
        )

    return TrialBalance(
        as_of=on_date,
        lines=tuple(lines),
        assets_gbp=assets_gbp,
        missing_prices=tuple(sorted(set(missing))),
        rate_gaps=tuple(gaps),
    )


def _fmt_amount(amount: Decimal) -> str:
    """Thousands-separated amount, trailing zeros trimmed (units can be
    fractional fund shares or whole-number lots)."""

    s = f"{amount:,f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _lines_for(tb: TrialBalance, acct_type: str) -> list[TrialBalanceLine]:
    return [line for line in tb.lines if line.type == acct_type]


def render_markdown(tb: TrialBalance) -> list[str]:
    """Render the trial balance as Markdown lines."""

    out = [
        "# Trial balance",
        "",
        f"_As of {tb.as_of.isoformat()}. Per-account closing balances from "
        "the beancount ledger (`bean-query`): securities in units, cash in "
        "native currency. The **Assets** / **Liabilities** sections add a "
        "GBP market-value column (latest mark, converted at the configured "
        "rate); Equity / Income / Expenses stay native (cumulative flows). "
        "Unvaluable balances are blank in the GBP column and listed below._",
        "",
    ]
    for acct_type in ACCOUNT_TYPES:
        type_lines = _lines_for(tb, acct_type)
        if not type_lines:
            continue
        valued = acct_type in VALUED_TYPES
        out.append(f"## {acct_type} ({len(type_lines)})")
        out.append("")
        if valued:
            out += ["| Account | Commodity | Balance | GBP (mkt) |",
                    "|---|---|---:|---:|"]
        else:
            out += ["| Account | Commodity | Balance |", "|---|---|---:|"]
        for line in type_lines:
            # One markdown row per commodity; the GBP cell (account-level)
            # sits on the first row.
            for i, (amount, commodity) in enumerate(line.native):
                cells = [f"`{line.account}`" if i == 0 else "",
                         commodity, _fmt_amount(amount)]
                if valued:
                    g = (gbp(line.value_gbp)
                         if i == 0 and line.value_gbp is not None else "")
                    cells.append(g)
                out.append("| " + " | ".join(cells) + " |")
        if valued:
            out += ["", f"**{acct_type} GBP (market):** {gbp(_type_gbp(tb, acct_type))}", ""]
        else:
            out.append("")

    if tb.missing_prices:
        out += ["## ⚠️ Unvaluable assets (no mark)", "",
                "These Asset/Liability accounts have no price in the ledger, "
                "so no GBP value — excluded from the GBP total:", "",
                *[f"- `{a}`" for a in tb.missing_prices], ""]
    if tb.rate_gaps:
        out += rate_gap_lines(
            tb.rate_gaps,
            title="Excluded from GBP — missing rate",
            intro="No GBP rate for these currency/month pairs; add the row "
            "to the HMRC monthly-average CSV to value them:",
        )
    return out


def _type_gbp(tb: TrialBalance, acct_type: str) -> Decimal:
    return sum(
        (line.value_gbp for line in _lines_for(tb, acct_type)
         if line.value_gbp is not None),
        Decimal(0),
    )


def render_csv_rows(tb: TrialBalance) -> list[list[str]]:
    """Rows for the CSV export: ``account, type, commodity, balance, gbp``."""

    rows: list[list[str]] = [["account", "type", "commodity", "balance", "gbp"]]
    for line in tb.lines:
        for i, (amount, commodity) in enumerate(line.native):
            g = (money(line.value_gbp)
                 if i == 0 and line.value_gbp is not None else "")
            rows.append([line.account, line.type, commodity, str(amount), g])
    return rows


def query_balances(ledger: Path) -> QueryResult:
    """Run the trial-balance query against ``ledger``."""

    return run_query(ledger, _BQL)
