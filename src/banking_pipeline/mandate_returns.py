"""Mandate return scorecard — step 2: time- and money-weighted returns.

What the Pictet mandate actually *returned*, computed **directly from the
statement holdings** so it needs no flow tagging in the ledger — the
deliberate work-around for the untagged deposits/withdrawals (the opening
capital, the 2023 top-ups, the 2025 Lombard drawdown) that would otherwise
distort the figures.

The trick: between two consecutive statements, a position's *market* gain
is ``qty_held × (price_now − price_then)`` — the price move on the units
held through **both** snapshots. A deposit (new units) or a withdrawal
(units sold) simply isn't in that sum, so external flows never read as
performance and the return is immune to whether they were tagged. What's
left over each period — ``ΔValue − market gain`` — is the **inferred
external flow**, surfaced as a "detected movements" table (the deposits /
withdrawals the holdings imply) and used to money-weight the MWR.

Two bases, side by side:

* **net (equity) return** — market gain over net worth (assets minus the
  negative Lombard cash). A leveraged book divides the same gain by a
  smaller equity base, so leverage amplifies it.
* **gross (asset) return** — market gain over the total asset book (the
  loan added back). The gap between the two is the leverage contribution.

For each basis, **TWR** (time-weighted — chained per-period market returns,
the manager's scorecard) and, for net, **MWR / XIRR** (money-weighted over
the inferred flows — the investor's actual experience).

Limitations (documented, not silently wrong): the holdings-based gain is a
*price* return, so income that a *distributing* fund pays out as cash —
rather than accumulating into its price — is treated as a small inferred
inflow, marginally understating total return; and an inferred flow lumps in
loan interest and dealing spreads alongside the genuine deposit/withdrawal.
Most of the book is accumulating funds, so the price return tracks total
return closely. A reporting aid, not advice.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from banking_pipeline.commodities_metadata import CommodityMetadata
from banking_pipeline.fx.gbp_rates import GbpRateSource
from banking_pipeline.report_format import gbp, money
from banking_pipeline.valuation import (
    RawHolding,
    as_of,
    raw_from_statement,
    value_holdings,
)

_ZERO = Decimal(0)
_PICTET_PREFIX = "Assets:Pic:"

# Inferred per-period flows below this magnitude are noise (dealing spreads,
# loan interest, distributed-fund income mistaken for a flow); only larger
# ones are surfaced as candidate deposits/withdrawals to tag.
_FLOW_REPORT_THRESHOLD = Decimal(50_000)


@dataclass(frozen=True)
class Snapshot:
    """A portfolio's value at one statement date, on both bases, plus the
    per-security GBP positions used for the holdings-based market gain."""

    portfolio: str
    on_date: date
    # key → (quantity, value_gbp); securities only (cash carries no mark).
    positions: dict[str, tuple[Decimal, Decimal]]
    net_value_gbp: Decimal  # net worth = assets − loan
    gross_value_gbp: Decimal  # total assets (loan added back)
    loan_gbp: Decimal  # ≤ 0; the negative (Lombard) cash total


@dataclass(frozen=True)
class DetectedFlow:
    """An external movement the holdings imply (``ΔValue − market gain``)."""

    portfolio: str
    on_date: date
    amount_gbp: Decimal  # signed: + into the portfolio (deposit), − out


@dataclass(frozen=True)
class ReturnSeries:
    """One portfolio's (or the aggregate's) return on net + gross bases."""

    label: str
    inception: date | None
    latest: date | None
    net_value_gbp: Decimal  # latest net worth
    gross_value_gbp: Decimal  # latest total assets
    twr_net: float | None  # cumulative, since inception
    twr_gross: float | None
    twr_net_annualised: float | None
    twr_gross_annualised: float | None
    mwr_net: float | None  # XIRR on the inferred flows + latest value


@dataclass(frozen=True)
class ReturnReport:
    aggregate: ReturnSeries
    per_portfolio: tuple[ReturnSeries, ...]
    detected_flows: tuple[DetectedFlow, ...]  # large inferred movements


# --- valuation snapshots ----------------------------------------------------


def build_snapshots(
    statements: list[tuple[str, str]],
    *,
    commodities: dict[str, CommodityMetadata],
    rate_source: GbpRateSource,
) -> list[Snapshot]:
    """Per-(portfolio, date) value snapshots for the Pictet mandate.

    Cash is netted *within* each portfolio (not across), so each account's
    own Lombard balance is its own loan. Property / ISA pseudo-portfolios
    are excluded — this is the Pictet mandate's return."""

    raws: list[RawHolding] = []
    for text, source in statements:
        raws.extend(raw_from_statement(text, source))

    groups: dict[tuple[str, date], dict[str, RawHolding]] = defaultdict(dict)
    for r in raws:
        if not r.portfolio.startswith(_PICTET_PREFIX):
            continue
        groups[(r.portfolio, r.on_date)][r.key] = r

    out: list[Snapshot] = []
    for (portfolio, on_date), grp in groups.items():
        valued = value_holdings(
            list(grp.values()), commodities=commodities, rate_source=rate_source
        )
        loan = sum(
            (c.value_gbp for c in valued.cash if c.value_gbp < _ZERO), _ZERO
        )
        positions = {
            h.key: (h.quantity, h.value_gbp)
            for h in valued.securities
            if h.quantity != _ZERO
        }
        # Drop an empty snapshot — a gap-period statement that parsed to no
        # holdings and no value. Left in, it would read as the whole book
        # leaving (a withdrawal) and coming back (a deposit) either side of
        # the gap; dropped, the timeline bridges the gap to the next real
        # statement (a genuine multi-month move). A legitimately all-cash
        # snapshot (the opening capital) has non-zero value, so it survives.
        if not positions and valued.net_worth_gbp == _ZERO:
            continue
        out.append(
            Snapshot(
                portfolio=portfolio,
                on_date=on_date,
                positions=positions,
                net_value_gbp=valued.net_worth_gbp,
                gross_value_gbp=valued.net_worth_gbp - loan,  # add the loan back
                loan_gbp=loan,
            )
        )
    out.sort(key=lambda s: (s.portfolio, s.on_date))
    return out


# --- holdings-based return maths --------------------------------------------


def _market_gain(prev: Snapshot, cur: Snapshot) -> Decimal:
    """GBP price gain on the securities held through *both* snapshots:
    ``Σ qty_prev × (gbp_unit_now − gbp_unit_then)``. Positions only in one
    snapshot (a buy or a sell during the period) are excluded — their value
    change is a flow/trade, not a market move."""

    gain = _ZERO
    for key, (qty_prev, val_prev) in prev.positions.items():
        cur_pos = cur.positions.get(key)
        if cur_pos is None:
            continue
        qty_cur, val_cur = cur_pos
        if qty_prev == _ZERO or qty_cur == _ZERO:
            continue
        unit_then = val_prev / qty_prev
        unit_now = val_cur / qty_cur
        gain += qty_prev * (unit_now - unit_then)
    return gain


def _chain(returns: list[float | None]) -> float | None:
    """Compound a list of sub-period returns (skipping gaps) into a cumulative
    return. ``None`` if no period is computable."""

    factor = 1.0
    any_period = False
    for r in returns:
        if r is None:
            continue
        factor *= 1.0 + r
        any_period = True
    return factor - 1.0 if any_period else None


def _annualise(cumulative: float | None, inception: date, latest: date) -> float | None:
    if cumulative is None:
        return None
    years = (latest - inception).days / 365.25
    if years <= 0:
        return cumulative
    return float((1.0 + cumulative) ** (1.0 / years)) - 1.0


def _xirr(cashflows: list[tuple[date, Decimal]]) -> float | None:
    """Money-weighted return: the rate ``r`` solving ``Σ cf / (1+r)^t = 0``,
    ``t`` in years from the first flow. Investor sign convention — deposits
    negative, withdrawals + ending value positive. Bisection (no SciPy);
    ``None`` when there is no sign change to bracket a root."""

    if len(cashflows) < 2:
        return None
    d0 = min(d for d, _ in cashflows)

    def npv(rate: float) -> float:
        return float(sum(
            float(a) / (1.0 + rate) ** ((d - d0).days / 365.25)
            for d, a in cashflows
        ))

    lo, hi = -0.9999, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo == 0:
        return lo
    if f_hi == 0:
        return hi
    if (f_lo > 0) == (f_hi > 0):
        return None  # no bracketed sign change
    for _ in range(200):
        mid = (lo + hi) / 2.0
        f_mid = npv(mid)
        if abs(f_mid) < 1e-7:
            return mid
        if (f_lo > 0) != (f_mid > 0):
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


def _series_for(
    label: str, snaps: list[Snapshot]
) -> tuple[ReturnSeries, list[DetectedFlow]]:
    """Holdings-based net + gross TWR and net MWR for one snapshot series,
    plus the inferred external flows. No flow tags needed: the period return
    is the market gain over the basis value, so deposits/withdrawals never
    enter the return — they emerge as the residual ``ΔValue − gain``."""

    snaps = sorted(snaps, key=lambda s: s.on_date)
    if len(snaps) < 2:
        last = snaps[-1] if snaps else None
        return (
            ReturnSeries(
                label=label,
                inception=snaps[0].on_date if snaps else None,
                latest=last.on_date if last else None,
                net_value_gbp=last.net_value_gbp if last else _ZERO,
                gross_value_gbp=last.gross_value_gbp if last else _ZERO,
                twr_net=None, twr_gross=None,
                twr_net_annualised=None, twr_gross_annualised=None,
                mwr_net=None,
            ),
            [],
        )

    net_returns: list[float | None] = []
    gross_returns: list[float | None] = []
    flows: list[DetectedFlow] = []

    for prev, cur in zip(snaps, snaps[1:], strict=False):
        gain = _market_gain(prev, cur)
        net_returns.append(
            float(gain / prev.net_value_gbp)
            if prev.net_value_gbp > _ZERO else None
        )
        gross_returns.append(
            float(gain / prev.gross_value_gbp)
            if prev.gross_value_gbp > _ZERO else None
        )
        # Inferred external flow: the value change the market gain doesn't
        # explain (a deposit adds value beyond price moves; a withdrawal
        # removes it). Includes loan interest / dealing spreads as noise.
        inferred = (cur.net_value_gbp - prev.net_value_gbp) - gain
        flows.append(DetectedFlow(label, cur.on_date, inferred))

    inception, latest = snaps[0].on_date, snaps[-1].on_date
    twr_net = _chain(net_returns)
    twr_gross = _chain(gross_returns)

    # MWR (net basis): inception equity out, each inferred flow, ending
    # equity in. Investor sign — a deposit (+ inferred flow) is cash out of
    # pocket (negative). Only meaningful from positive starting equity.
    mwr_net: float | None = None
    if snaps[0].net_value_gbp > _ZERO:
        cashflows: list[tuple[date, Decimal]] = [
            (inception, -snaps[0].net_value_gbp)
        ]
        cashflows += [(f.on_date, -f.amount_gbp) for f in flows]
        cashflows.append((latest, snaps[-1].net_value_gbp))
        mwr_net = _xirr(cashflows)

    return (
        ReturnSeries(
            label=label,
            inception=inception,
            latest=latest,
            net_value_gbp=snaps[-1].net_value_gbp,
            gross_value_gbp=snaps[-1].gross_value_gbp,
            twr_net=twr_net,
            twr_gross=twr_gross,
            twr_net_annualised=_annualise(twr_net, inception, latest),
            twr_gross_annualised=_annualise(twr_gross, inception, latest),
            mwr_net=mwr_net,
        ),
        flows,
    )


def _aggregate_snapshots(snaps: list[Snapshot]) -> list[Snapshot]:
    """Combine per-portfolio snapshots into a whole-mandate series via the
    as-of forward-fill (each portfolio contributes its latest snapshot on or
    before each date). Positions are keyed by ``portfolio|key`` so a holding
    a newly-statemented portfolio brings is a *new* key — excluded from that
    period's market gain, so a portfolio first appearing (a coverage gap) is
    a flow, never a spurious return."""

    by_portfolio: dict[str, list[Snapshot]] = defaultdict(list)
    for s in snaps:
        by_portfolio[s.portfolio].append(s)
    for lst in by_portfolio.values():
        lst.sort(key=lambda s: s.on_date)

    out: list[Snapshot] = []
    for d in sorted({s.on_date for s in snaps}):
        net = gross = loan = _ZERO
        positions: dict[str, tuple[Decimal, Decimal]] = {}
        for portfolio, lst in by_portfolio.items():
            chosen = as_of(lst, d, key=lambda s: s.on_date)
            if chosen is None:
                continue
            net += chosen.net_value_gbp
            gross += chosen.gross_value_gbp
            loan += chosen.loan_gbp
            for key, pos in chosen.positions.items():
                positions[f"{portfolio}|{key}"] = pos
        out.append(Snapshot("Pictet (all)", d, positions, net, gross, loan))
    return out


def build_report(
    statements: list[tuple[str, str]],
    *,
    commodities: dict[str, CommodityMetadata],
    rate_source: GbpRateSource,
) -> ReturnReport:
    """Assemble the whole-mandate and per-portfolio holdings-based returns."""

    snaps = build_snapshots(
        statements, commodities=commodities, rate_source=rate_source
    )

    by_portfolio: dict[str, list[Snapshot]] = defaultdict(list)
    for s in snaps:
        by_portfolio[s.portfolio].append(s)

    per_portfolio: list[ReturnSeries] = []
    for p in sorted(by_portfolio):
        series, _ = _series_for(p, by_portfolio[p])
        per_portfolio.append(series)

    aggregate, agg_flows = _series_for("Pictet (all)", _aggregate_snapshots(snaps))
    detected = tuple(
        f for f in agg_flows if abs(f.amount_gbp) >= _FLOW_REPORT_THRESHOLD
    )
    return ReturnReport(
        aggregate=aggregate,
        per_portfolio=tuple(per_portfolio),
        detected_flows=detected,
    )


# --- rendering --------------------------------------------------------------


def _pctf(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _portfolio_label(key: str) -> str:
    """``Assets:Pic:K999999001`` → ``K999999001`` for display."""

    return key.rsplit(":", 1)[-1]


def _series_row(s: ReturnSeries, label: str) -> str:
    return (
        f"| {label} | {gbp(s.net_value_gbp)} | {_pctf(s.twr_net)} "
        f"| {_pctf(s.twr_net_annualised)} | {_pctf(s.mwr_net)} "
        f"| {_pctf(s.twr_gross)} | {_pctf(s.twr_gross_annualised)} |"
    )


def render_markdown(report: ReturnReport) -> str:
    agg = report.aggregate
    span = (
        f"{agg.inception} → {agg.latest}"
        if agg.inception and agg.latest
        else "—"
    )
    lines = [
        "# Mandate returns — time- & money-weighted",
        "",
        f"Pictet mandate return over **{span}**, computed **from the statement "
        "holdings** (price moves on the units held through each pair of "
        "statements), so it needs no flow tagging in the ledger — deposits "
        "and withdrawals never read as performance. Two bases: **net** (over "
        "your equity, assets minus the Lombard loan) and **gross** (over the "
        "total asset book, loan added back); their gap is the leverage "
        "contribution. TWR is the manager's scorecard; MWR/XIRR (over the "
        "inferred flows) is your money-weighted experience. A reporting aid, "
        "not advice.",
        "",
        "| Scope | Latest net worth | TWR (net) | TWR p.a. (net) | MWR (net) "
        "| TWR (gross) | TWR p.a. (gross) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        _series_row(agg, "**Pictet (all)**"),
    ]
    for s in report.per_portfolio:
        lines.append(_series_row(s, _portfolio_label(s.label)))
    lines.append("")
    lines += [
        "*Net vs gross TWR p.a.* shows the leverage contribution: where net "
        "exceeds gross the loan amplified gains; where it trails, leverage "
        "dragged. *MWR vs TWR* shows how deposit/withdrawal timing helped or "
        "hurt your realised return versus the manager's underlying one.",
        "",
    ]
    if any(s.net_value_gbp < _ZERO for s in report.per_portfolio):
        lines += [
            "> An account whose Lombard loan exceeds its assets has negative "
            "equity, so its **net** return is undefined and shown `—` (only "
            "the whole-mandate net, where the loan nets against the other "
            "account's assets, is meaningful). Its gross figure values the "
            "asset side alone.",
            "",
        ]

    if report.detected_flows:
        lines += [
            "## Detected movements (inferred from the holdings)",
            "",
            "These deposits / withdrawals are what the holdings imply each "
            "period (`ΔValue − market gain`) — the returns above already "
            "exclude them, so tagging them in the ledger is optional and "
            "only firms up the money-weighted figure. A positive amount is "
            "money **in**, negative is **out**:",
            "",
            "| Date | Amount | Direction |",
            "| --- | ---: | --- |",
        ]
        for f in report.detected_flows:
            direction = "deposit" if f.amount_gbp > _ZERO else "withdrawal"
            lines.append(
                f"| {f.on_date} | {gbp(abs(f.amount_gbp))} | {direction} |"
            )
        lines += [
            "",
            "> An inferred flow also absorbs loan interest, dealing spreads "
            "and any cash income a *distributing* fund pays out rather than "
            "accumulating, so treat the amounts as indicative.",
            "",
        ]

    return "\n".join(lines).rstrip() + "\n"


def render_csv_rows(report: ReturnReport) -> list[list[str]]:
    rows: list[list[str]] = [[
        "scope", "inception", "latest", "net_worth_gbp", "gross_assets_gbp",
        "twr_net", "twr_net_annualised", "mwr_net",
        "twr_gross", "twr_gross_annualised",
    ]]

    def _row(s: ReturnSeries, label: str) -> list[str]:
        def f(v: float | None) -> str:
            return "" if v is None else f"{v:.6f}"
        return [
            label,
            s.inception.isoformat() if s.inception else "",
            s.latest.isoformat() if s.latest else "",
            money(s.net_value_gbp), money(s.gross_value_gbp),
            f(s.twr_net), f(s.twr_net_annualised), f(s.mwr_net),
            f(s.twr_gross), f(s.twr_gross_annualised),
        ]

    rows.append(_row(report.aggregate, "Pictet (all)"))
    for s in report.per_portfolio:
        rows.append(_row(s, _portfolio_label(s.label)))
    return rows
