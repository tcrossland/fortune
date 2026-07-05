"""Parse Pictet's "Income and capital gains UK" report (its own GBP,
Section-104 UK-tax computation) for a reconciliation cross-check.

This is Pictet's annual **UK Tax Report** — a GBP, per-account, Section 104
computation of capital gains and overseas income, translated from the trade
currency at Pictet's *average* exchange rate. We parse three things from the
extracted PDF text:

- the **capital-gain overview** totals (chargeable gain before / on-or-after the
  30 October 2024 rate-change date, allowable loss, deep-discounted, offshore
  income gains, exempt) — these line up with the pipeline's SA108 buckets;
- the **per-security** capital-gain detail (name, ISIN, cost/proceeds/gain in
  GBP) — the presence check that catches a missing or extra disposal;
- the **overseas income** totals (interest / dividend gross + withholding tax).

It is a *cross-check* input, never fed to the tax pipeline: the SA108 / SA106
figures stay computed from the JSONL sidecars. Pictet's average FX + per-account
pooling mean the figures won't tie to the penny — the caller compares the
aggregate within tolerance and the per-security *presence* exactly. Not tax
advice. See [docs/plans/pictet-uk-cgt-report.md].
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# A GBP amount: optional leading minus, thousands commas, always 2 dp.
_AMOUNT = r"-?[\d,]+\.\d{2}"
_AMOUNT_RE = re.compile(_AMOUNT)
# A real ISIN is 2 letters + 10 alphanumerics (checksum not enforced here —
# the report only prints valid ISINs, and the metadata layer validates ours).
_ISIN = r"[A-Z]{2}[A-Z0-9]{10}"

# A per-security summary line: "<name> <ISIN> <cost> <proceeds> <gain/loss>".
_SECURITY_RE = re.compile(
    rf"^(?P<name>.+?)\s+(?P<isin>{_ISIN})\s+"
    rf"(?P<cost>{_AMOUNT})\s+(?P<proceeds>{_AMOUNT})\s+(?P<gain>{_AMOUNT})\s*$"
)

# The capital-gain overview labels, in the report's own wording. Each label sits
# on its own line with the amount on the following line.
_OVERVIEW_LABELS: dict[str, str] = {
    "gain_pre": "Total chargeable gain (disposals before 30 October 2024)",
    "gain_post": "Total chargeable gain (disposals on or after 30 October 2024)",
    "allowable_loss": "Total allowable loss",
    "deep_discounted_gain": "Total deep discounted gains taxed to income",
    "deep_discounted_loss": "Total deep discounted securities losses",
    "offshore_income_gain": "Total offshore gains taxed to income",
    "exempt": "Total exempt gain / loss",
}


def _amount(token: str) -> Decimal | None:
    try:
        return Decimal(token.replace(",", ""))
    except InvalidOperation:
        return None


@dataclass(frozen=True)
class CgSecurity:
    """One security's aggregate capital-gain line (GBP). ``estimated_cost`` is
    true when Pictet shows a zero allowable cost — a security it lacks the
    acquisition history for (which the pipeline may cost correctly via
    ``opening-positions.toml``), so a divergence there is expected, not a bug."""

    name: str
    isin: str
    cost_gbp: Decimal
    proceeds_gbp: Decimal
    gain_loss_gbp: Decimal

    @property
    def estimated_cost(self) -> bool:
        return self.cost_gbp == 0 and self.proceeds_gbp != 0


@dataclass(frozen=True)
class CgOverview:
    """The capital-gain overview totals (GBP)."""

    gain_pre: Decimal
    gain_post: Decimal
    allowable_loss: Decimal  # signed (≤ 0 as printed)
    deep_discounted_gain: Decimal
    deep_discounted_loss: Decimal
    offshore_income_gain: Decimal
    exempt: Decimal


@dataclass(frozen=True)
class IncomeTotals:
    """Overseas income aggregates (GBP): the ``TOTAL Overseas …`` lines."""

    interest_gross: Decimal
    interest_wht: Decimal
    dividend_gross: Decimal
    dividend_wht: Decimal


@dataclass(frozen=True)
class UkTaxReport:
    overview: CgOverview | None
    securities: tuple[CgSecurity, ...]
    income: IncomeTotals | None


def _parse_overview(lines: list[str]) -> CgOverview | None:
    """Read the labelled overview totals (label line, amount on the next
    non-blank line — the FY24-25 layout).

    Fail-safe by design: a label whose amount is on the *same* line, wraps, or
    is separated by an intervening note is silently skipped (→ a zero, or the
    whole overview ``None`` if no ``gain_*`` label resolves). Since these
    figures are non-gating (tolerance-matched), a future report-format change
    degrades to "no aggregate check" rather than a wrong number — but if Pictet
    changes the layout, that is the place to look."""

    found: dict[str, Decimal] = {}
    for key, label in _OVERVIEW_LABELS.items():
        for i, line in enumerate(lines):
            if line.strip() == label:
                # The amount is the next non-blank line.
                for nxt in lines[i + 1 :]:
                    token = nxt.strip()
                    if not token:
                        continue
                    amount = _amount(token) if _AMOUNT_RE.fullmatch(token) else None
                    if amount is not None:
                        found[key] = amount
                    break
                break
    if "gain_pre" not in found and "gain_post" not in found:
        return None  # no overview section recognised
    z = Decimal(0)
    return CgOverview(
        gain_pre=found.get("gain_pre", z),
        gain_post=found.get("gain_post", z),
        allowable_loss=found.get("allowable_loss", z),
        deep_discounted_gain=found.get("deep_discounted_gain", z),
        deep_discounted_loss=found.get("deep_discounted_loss", z),
        offshore_income_gain=found.get("offshore_income_gain", z),
        exempt=found.get("exempt", z),
    )


def _parse_securities(lines: list[str]) -> list[CgSecurity]:
    """Per-security summary lines (name + ISIN + cost/proceeds/gain). The
    per-disposal ``Sale …`` lines below each security are skipped — the summary
    line carries the security total the cross-check needs."""

    out: list[CgSecurity] = []
    seen: set[str] = set()
    for line in lines:
        m = _SECURITY_RE.match(line.strip())
        if m is None:
            continue
        isin = m["isin"]
        if isin in seen:  # a security appears once as a summary line
            continue
        cost = _amount(m["cost"])
        proceeds = _amount(m["proceeds"])
        gain = _amount(m["gain"])
        if cost is None or proceeds is None or gain is None:
            continue
        seen.add(isin)
        out.append(
            CgSecurity(
                name=m["name"].strip(), isin=isin, cost_gbp=cost,
                proceeds_gbp=proceeds, gain_loss_gbp=gain,
            )
        )
    return out


def _parse_income(lines: list[str]) -> IncomeTotals | None:
    """The two ``TOTAL Overseas …`` lines: label then gross, WHT, received,
    equalisation (we keep gross + WHT)."""

    def _totals(prefix: str) -> tuple[Decimal, Decimal] | None:
        for line in lines:
            s = line.strip()
            if s.startswith(prefix):
                amounts = _AMOUNT_RE.findall(s)
                if len(amounts) >= 2:
                    gross = _amount(amounts[0])
                    wht = _amount(amounts[1])
                    if gross is not None and wht is not None:
                        return gross, wht
        return None

    interest = _totals("TOTAL Overseas Interest")
    dividend = _totals("TOTAL Overseas Dividend")
    if interest is None and dividend is None:
        return None
    z = (Decimal(0), Decimal(0))
    ig, iw = interest or z
    dg, dw = dividend or z
    return IncomeTotals(
        interest_gross=ig, interest_wht=iw, dividend_gross=dg, dividend_wht=dw
    )


def parse_uk_tax_report(text: str) -> UkTaxReport:
    """Parse the extracted text of a Pictet UK Tax Report."""

    lines = text.splitlines()
    return UkTaxReport(
        overview=_parse_overview(lines),
        securities=tuple(_parse_securities(lines)),
        income=_parse_income(lines),
    )


# --- cross-check against the computed SA108 / SA106 ------------------------


@dataclass(frozen=True)
class Finding:
    """One reconciliation line. ``pictet`` / ``pipeline`` are ``None`` for a
    presence finding (a security in one side only). ``status`` is one of
    ``match`` / ``mismatch`` / ``pictet_only`` / ``pipeline_only``."""

    category: str  # "capital-gains" | "income" | "presence"
    label: str
    status: str
    pictet: Decimal | None = None
    pipeline: Decimal | None = None


@dataclass(frozen=True)
class PipelineFigures:
    """The pipeline's computed side, pre-aggregated by the caller.

    ``interest_gross`` / ``dividend_gross`` must **include ERI** (excess
    reportable income) — Pictet's report folds reporting-fund income into its
    overseas interest / dividend totals, whereas the pipeline splits it into
    SA106 rows + a separate ERI result, so the caller re-combines them before
    comparing. ``disposal_isins`` is every ISIN with a disposal (SA108 +
    deep-discounted)."""

    chargeable_gains: Decimal  # positive CGT gains
    allowable_loss: Decimal  # ≤ 0
    offshore_income_gain: Decimal
    interest_gross: Decimal
    dividend_gross: Decimal
    disposal_isins: frozenset[str]


def _cmp(
    category: str,
    label: str,
    pictet: Decimal,
    pipeline: Decimal,
    *,
    abs_tol: Decimal,
    pct_tol: Decimal,
) -> Finding:
    """Tolerance compare: within max(abs_tol, pct_tol × larger magnitude)."""

    tol = max(abs_tol, pct_tol * max(abs(pictet), abs(pipeline)))
    status = "match" if abs(pictet - pipeline) <= tol else "mismatch"
    return Finding(category, label, status, pictet, pipeline)


def reconcile_uk_tax(
    report: UkTaxReport,
    pipeline: PipelineFigures,
    *,
    abs_tol: Decimal = Decimal("50"),
    pct_tol: Decimal = Decimal("0.10"),
) -> list[Finding]:
    """Cross-check Pictet's parsed UK Tax Report against the pipeline's figures.

    Aggregates are compared within a **tolerance** (Pictet uses an average FX +
    per-account pooling, so they won't tie to the penny); each security's
    **presence** is compared exactly (a disposal in one side only is the real
    bug signal — a missing/extra trade or a routing difference). The report is
    per-account, so against the NIF-level pool a `pipeline-only` security may
    just be the other mandate — treat presence as a lead, not a verdict.
    """

    out: list[Finding] = []

    if report.overview is not None:
        ov = report.overview
        out += [
            _cmp("capital-gains", "chargeable gains", ov.gain_pre + ov.gain_post,
                 pipeline.chargeable_gains, abs_tol=abs_tol, pct_tol=pct_tol),
            _cmp("capital-gains", "allowable loss", ov.allowable_loss,
                 pipeline.allowable_loss, abs_tol=abs_tol, pct_tol=pct_tol),
            _cmp("capital-gains", "offshore income gains", ov.offshore_income_gain,
                 pipeline.offshore_income_gain, abs_tol=abs_tol, pct_tol=pct_tol),
        ]

    if report.income is not None:
        inc = report.income
        out += [
            _cmp("income", "overseas interest (gross)", inc.interest_gross,
                 pipeline.interest_gross, abs_tol=abs_tol, pct_tol=pct_tol),
            _cmp("income", "overseas dividends (gross)", inc.dividend_gross,
                 pipeline.dividend_gross, abs_tol=abs_tol, pct_tol=pct_tol),
        ]

    pictet_isins = {s.isin for s in report.securities}
    for isin in sorted(pictet_isins - pipeline.disposal_isins):
        out.append(Finding("presence", isin, "pictet_only"))
    for isin in sorted(pipeline.disposal_isins - pictet_isins):
        out.append(Finding("presence", isin, "pipeline_only"))
    return out


def has_material_finding(findings: list[Finding]) -> bool:
    """True only when Pictet booked a disposal the pipeline is missing
    (``pictet_only``) — the tax-critical, FX-independent signal that gates the
    build. Aggregate **mismatches are deliberately *not* material**: Pictet's
    average FX + per-account pooling make the totals diverge (a small figure
    like the allowable loss can swing tens of percent on FX alone), so gating on
    them would cry wolf every year. They stay visible in the report for review.
    ``pipeline_only`` isn't material either (likely the other mandate)."""

    return any(f.status == "pictet_only" for f in findings)
