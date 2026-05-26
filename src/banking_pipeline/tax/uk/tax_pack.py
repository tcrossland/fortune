"""Per-year UK 'tax pack' — a single Markdown document that ties the
computed SA108 / SA106 figures to the boxes on the actual HMRC forms.

The CSVs that ``tax-report`` writes are the machine-readable substrate;
this turns them into one human-readable filing aid, grouping each figure
under the right HMRC schedule + section and naming the box to enter it
in. It is a *filing aid, not tax advice* — and HMRC re-numbers the forms
periodically, so the box numbers are indicative for recent years and
carry a verify-against-the-form caveat in the rendered header.

The renderer is pure: it takes the already-computed report objects and
returns Markdown text, so the CLI owns all I/O and orchestration.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from banking_pipeline.tax.uk.cgt_allowance import CGT_STATUSES, CgtAllowanceResult
from banking_pipeline.tax.uk.currency import RateGap
from banking_pipeline.tax.uk.eri import EriResult
from banking_pipeline.tax.uk.residence import FigDesignationRow, fig_subtotals
from banking_pipeline.tax.uk.sa106 import Sa106Report
from banking_pipeline.tax.uk.sa108 import Sa108Report

_ZERO = Decimal(0)


def _gbp(value: Decimal) -> str:
    return f"£{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,}"


def _sum(values: list[Decimal]) -> Decimal:
    return sum(values, _ZERO)


def render_tax_pack(
    *,
    year: str,
    sa108: Sa108Report,
    sa106: Sa106Report,
    eri: EriResult,
    allowance: CgtAllowanceResult,
    designation: list[FigDesignationRow],
    fig_claimed: bool,
    rate_change_date: date | None = None,
    rate_gaps: list[RateGap] | None = None,
) -> str:
    """Render the per-year tax pack as Markdown.

    ``designation`` is the FIG-relieved rows (empty unless
    ``fig_claimed``); ``rate_gaps`` are amounts that couldn't be converted
    to GBP (so the figures understate).
    """

    lines: list[str] = [
        f"# UK tax pack — {year}",
        "",
        "A filing aid generated from the ingested statements — **not tax "
        "advice**, and not a substitute for the return. HMRC re-numbers the "
        "forms periodically, so the **box numbers are indicative for recent "
        f"years; verify each against the SA108 / SA106 for {year}**. All "
        "figures are GBP.",
        "",
    ]

    lines += _capital_gains_section(sa108, allowance, rate_change_date)
    lines += _foreign_dividends_section(sa106, eri)
    lines += _foreign_interest_section(sa106, eri)
    lines += _offshore_income_gains_section(sa108)
    lines += _deep_discounted_section(sa108)
    if fig_claimed:
        lines += _fig_section(designation)
        lines += _unclassified_fig_section(sa108)
    lines += _coverage_section(rate_gaps or [])

    return "\n".join(lines).rstrip() + "\n"


def _capital_gains_section(
    sa108: Sa108Report,
    allowance: CgtAllowanceResult,
    rate_change_date: date | None,
) -> list[str]:
    cgt = [r for r in sa108.rows if r.reporting_status in CGT_STATUSES]
    proceeds = _sum([r.proceeds_gbp for r in cgt])
    costs = _sum([r.cost_gbp for r in cgt])
    gains = _sum([r.gain_gbp for r in cgt if r.gain_gbp > 0])
    losses = _sum([-r.gain_gbp for r in cgt if r.gain_gbp < 0])

    lines = [
        "## Capital gains — SA108",
        "",
        'Listed shares and securities (the "Listed shares and securities" '
        "section):",
        "",
        "| Box | Description | Amount |",
        "| --- | --- | --- |",
        f"| 23 | Number of disposals | {len(cgt)} |",
        f"| 24 | Disposal proceeds | {_gbp(proceeds)} |",
        f"| 25 | Allowable costs (incl. purchase price) | {_gbp(costs)} |",
        f"| 26 | Gains in the year, before losses | {_gbp(gains)} |",
        f"| 27 | Losses in the year | {_gbp(losses)} |",
        "",
        "Computation — HMRC applies the allowance and losses; keep this as "
        "your working sheet:",
        "",
        f"- Net gain after current-year losses: {_gbp(allowance.net_gain)}",
        f"- Brought-forward losses used: {_gbp(allowance.brought_forward_used)}",
        f"- Annual exempt amount: {_gbp(allowance.annual_exempt_amount)}",
    ]
    if allowance.rate_split and rate_change_date is not None:
        label = f"{rate_change_date.day} {rate_change_date:%b %Y}"
        lines += [
            f"- Taxable gain before {label} (lower rate): "
            f"{_gbp(allowance.taxable_pre)}",
            f"- Taxable gain on/after {label} (higher rate): "
            f"{_gbp(allowance.taxable_post)}",
            f"- Taxable gain (total): {_gbp(allowance.taxable_total)}",
        ]
    else:
        lines.append(f"- Taxable gain: {_gbp(allowance.taxable_total)}")
    lines += [
        f"- Losses carried forward: {_gbp(allowance.losses_carried_forward)}",
        "",
    ]
    for w in allowance.expired_loss_claims:
        deadline = f"{w.deadline.day} {w.deadline:%b %Y}"
        lines.append(
            f"> ⚠️ **Loss-claim window:** the {_gbp(w.amount_used)} loss from "
            f"{w.arising_year} relieved here arose over 4 years ago — it is "
            f"allowable only if it was notified to HMRC by {deadline}. Confirm "
            "before relying on it (the figures above are unadjusted)."
        )
    if allowance.expired_loss_claims:
        lines.append("")
    return lines


def _foreign_dividends_section(sa106: Sa106Report, eri: EriResult) -> list[str]:
    eri_div = [r for r in eri.rows if r.income_type == "dividend"]
    gross = _sum([r.gross_gbp for r in sa106.dividends]) + _sum(
        [r.gross_gbp for r in eri_div]
    )
    wht = _sum([r.wht_gbp for r in sa106.dividends])
    lines = [
        "## Foreign dividends — SA106",
        "",
        'Enter under "Dividends from foreign companies". Total includes '
        "excess reportable income taxed as a dividend.",
        "",
        f"- Total gross dividends: {_gbp(gross)}",
        f"- Foreign tax (for Foreign Tax Credit Relief): {_gbp(wht)}",
    ]
    if sa106.dividends:
        lines += ["", "| Country | ISIN | Gross | Foreign tax |", "| --- | --- | --- | --- |"]
        for r in sa106.dividends:
            lines.append(
                f"| {r.country} | {r.isin} | {_gbp(r.gross_gbp)} | "
                f"{_gbp(r.wht_gbp)} |"
            )
    if eri_div:
        lines.append("")
        lines.append(
            f"Of which excess reportable income (accumulating funds): "
            f"{_gbp(_sum([r.gross_gbp for r in eri_div]))}."
        )
    lines.append("")
    return lines


def _foreign_interest_section(sa106: Sa106Report, eri: EriResult) -> list[str]:
    eri_int = [r for r in eri.rows if r.income_type == "interest"]
    gross = _sum([r.gross_gbp for r in sa106.interest]) + _sum(
        [r.gross_gbp for r in eri_int]
    )
    wht = _sum([r.wht_gbp for r in sa106.interest])
    if not sa106.interest and not eri_int:
        return []
    lines = [
        "## Foreign interest — SA106",
        "",
        'Enter under "Interest and other income from overseas savings". '
        "Distributions from >60%-interest-bearing offshore (bond) funds and "
        "their excess reportable income are foreign interest, not dividends.",
        "",
        f"- Total gross interest: {_gbp(gross)}",
        f"- Foreign tax (for Foreign Tax Credit Relief): {_gbp(wht)}",
        "",
    ]
    return lines


def _offshore_income_gains_section(sa108: Sa108Report) -> list[str]:
    oig = [r for r in sa108.rows if r.reporting_status == "non-reporting"]
    if not oig:
        return []
    gain = _sum([r.gain_gbp for r in oig if r.gain_gbp > 0])
    return [
        "## Offshore income gains — SA106",
        "",
        "Gains on disposals of non-reporting offshore funds are taxed as "
        'income, not CGT — enter under "Other overseas income and gains" '
        "(offshore funds).",
        "",
        f"- Total offshore income gain: {_gbp(gain)}",
        "",
    ]


def _deep_discounted_section(sa108: Sa108Report) -> list[str]:
    dds = sa108.dds_disposals
    if not dds:
        return []
    income = _sum([r.gain_gbp for r in dds if r.gain_gbp > 0])
    return [
        "## Deeply discounted securities",
        "",
        "The profit is taxed as income (not CGT); a loss is generally not "
        "allowable. Enter on the additional information pages (SA101), "
        '"Deeply discounted securities".',
        "",
        f"- Profit taxed as income: {_gbp(income)}",
        "",
    ]


def _fig_section(designation: list[FigDesignationRow]) -> list[str]:
    sub = fig_subtotals(designation)
    lines = [
        "## Foreign Income & Gains (FIG) claim — SA109",
        "",
        "A 4-year FIG claim is being made: the foreign income and non-UK "
        "gains below are relieved, but the personal allowance and the CGT "
        "annual exempt amount are forfeited for the year. Claim on the "
        "residence pages (SA109) and designate the amounts.",
        "",
        f"- Foreign income relieved: {_gbp(sub.income)}",
        f"- Non-UK gains relieved: {_gbp(sub.gains)}",
        f"- Disallowed foreign losses (loss relief forfeited): {_gbp(sub.losses)}",
        f"- Net foreign income + gains relieved: {_gbp(sub.net)}",
        "",
    ]
    if sub.losses < _ZERO:
        lines += [
            "A FIG claim relieves foreign gains but **also forfeits relief "
            "for foreign losses** in the year — the disallowed losses above "
            "cannot be set against other gains or carried forward. Weigh "
            "this against the income/gains relieved.",
            "",
        ]
    lines += [
        "| Kind | Category | Country | ISIN | Amount |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in designation:
        lines.append(
            f"| {r.kind} | {r.category} | {r.country} | {r.isin} "
            f"| {_gbp(r.amount_gbp)} |"
        )
    lines.append("")
    return lines


def _unclassified_fig_section(sa108: Sa108Report) -> list[str]:
    """Under a FIG claim, disposals with no ``commodities.toml`` entry
    default to UK situs, so they are neither reported as a gain nor
    relieved — and a genuinely foreign one is silently missing relief.
    List them so the metadata can be fixed before filing. (Only rendered
    when a claim is active; the caller gates on ``fig_claimed``.)"""

    unknown = {
        r.isin: r.commodity_name
        for r in sa108.rows
        if r.reporting_status == "unknown"
    }
    if not unknown:
        return []
    lines = [
        "## ⚠️ Unclassified holdings — may be missing FIG relief",
        "",
        "These disposals have no `data/commodities.toml` entry, so they "
        "default to **UK situs** — under the FIG claim they are neither "
        "reported as a gain nor relieved. If any is actually a foreign "
        "holding it is **missing relief** and should be designated on the "
        "FIG pages. Confirm each one's situs and add it to "
        "`commodities.toml` before filing:",
        "",
    ]
    for isin in sorted(unknown):
        name = unknown[isin]
        lines.append(f"- {isin}" + (f" — {name}" if name and name != isin else ""))
    lines.append("")
    return lines


def _coverage_section(gaps: list[RateGap]) -> list[str]:
    if not gaps:
        return []
    uniq = sorted(set(gaps), key=lambda g: (g.currency, g.month, g.isin))
    lines = [
        "## ⚠️ Incomplete — missing GBP rates",
        "",
        "Some amounts could not be converted to GBP and are **excluded** "
        "from the figures above, so this pack understates. Add the "
        "month/currency to `data/fx/hmrc-monthly-average.csv` (or stamp the "
        "transaction's `gbp-rate`) and regenerate:",
        "",
    ]
    lines += [f"- {g.currency} {g.month} ({g.isin})" for g in uniq]
    lines.append("")
    return lines
