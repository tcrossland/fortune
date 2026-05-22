"""UK share-identification (section 104) matching for a single security.

Applies HMRC's three matching rules, in order, to the disposals of one
ISIN:

1. **Same-day rule** — a disposal matches acquisitions made on the same
   day first.
2. **Bed-and-breakfast (30-day) rule** — it then matches acquisitions in
   the 30 days *following* the disposal.
3. **Section 104 pool** — anything left draws from the pool, a running
   quantity-weighted average GBP cost of every share not consumed by the
   first two rules.

All amounts are GBP (the caller converts before building the inputs —
see :mod:`banking_pipeline.tax.uk.currency`). A single disposal can be
satisfied from more than one bucket, producing one
:class:`MatchedDisposal` per bucket portion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, cast

MatchType = Literal["same-day", "bed-and-breakfast", "s104"]

_PENNY = Decimal("0.01")


def _round(value: Decimal) -> Decimal:
    return value.quantize(_PENNY, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Acquisition:
    """One purchase of a security: ``cost_gbp`` is the total allowable
    cost in GBP (consideration plus acquisition costs) for ``qty`` units."""

    date: date
    qty: Decimal
    cost_gbp: Decimal


@dataclass(frozen=True)
class Disposal:
    """One sale: ``proceeds_gbp`` is the total disposal proceeds in GBP
    (net of disposal costs) for ``qty`` units."""

    date: date
    qty: Decimal
    proceeds_gbp: Decimal


@dataclass(frozen=True)
class PoolCostAdjustment:
    """A dated change to the section 104 pool's cost without changing its
    quantity — used to fold excess reportable income (net of
    equalisation) into the base cost of the units held on ``date``.
    Positive raises the pool's allowable cost; negative lowers it."""

    date: date
    cost_gbp: Decimal


@dataclass(frozen=True)
class MatchedDisposal:
    """One disposal portion matched to a single bucket.

    ``cost_gbp`` is the allowable cost attributed to ``disposal_qty``
    units; ``gain_gbp = proceeds_gbp - cost_gbp`` (negative = a loss).
    ``acquisition_dates`` lists the acquisition date(s) that supplied the
    cost — empty for the section 104 pool, which is an aggregate.
    """

    disposal_date: date
    disposal_qty: Decimal
    proceeds_gbp: Decimal
    cost_gbp: Decimal
    gain_gbp: Decimal
    matched_against: MatchType
    acquisition_dates: list[date]


@dataclass
class _Lot:
    date: date
    qty: Decimal
    total_gbp: Decimal
    remaining: Decimal = field(init=False)

    def __post_init__(self) -> None:
        self.remaining = self.qty

    @property
    def unit(self) -> Decimal:
        return self.total_gbp / self.qty


def _match(
    disp: _Lot, acq: _Lot, bucket: MatchType, out: list[MatchedDisposal]
) -> None:
    """Match as much of ``disp`` against ``acq`` as both have left."""

    qty = min(disp.remaining, acq.remaining)
    if qty <= 0:
        return
    proceeds = disp.unit * qty
    cost = acq.unit * qty
    out.append(
        MatchedDisposal(
            disposal_date=disp.date,
            disposal_qty=qty,
            proceeds_gbp=_round(proceeds),
            cost_gbp=_round(cost),
            gain_gbp=_round(proceeds - cost),
            matched_against=bucket,
            acquisition_dates=[acq.date],
        )
    )
    disp.remaining -= qty
    acq.remaining -= qty


def match_disposals(
    acquisitions: list[Acquisition],
    disposals: list[Disposal],
    cost_adjustments: list[PoolCostAdjustment] | None = None,
) -> list[MatchedDisposal]:
    """Match every disposal of one ISIN and return the per-bucket records.

    Disposals are reported in chronological order; a disposal split
    across buckets yields several records in same-day → 30-day → s104
    order.

    ``cost_adjustments`` are dated pool-cost changes (ERI net of
    equalisation) applied to the section 104 pool on their date, before
    any same-date disposal draws from it. They never affect same-day or
    30-day matching and are ignored when the pool is empty.
    """

    acqs = sorted(
        (_Lot(a.date, a.qty, a.cost_gbp) for a in acquisitions),
        key=lambda lot: lot.date,
    )
    disps = sorted(
        (_Lot(d.date, d.qty, d.proceeds_gbp) for d in disposals),
        key=lambda lot: lot.date,
    )
    out: list[MatchedDisposal] = []

    # 1. Same-day.
    for disp in disps:
        for acq in acqs:
            if acq.date == disp.date:
                _match(disp, acq, "same-day", out)

    # 2. Bed-and-breakfast: acquisitions in the 30 days *after* the
    #    disposal, earliest first.
    for disp in disps:
        if disp.remaining <= 0:
            continue
        for acq in acqs:
            if disp.date < acq.date <= disp.date + timedelta(days=30):
                _match(disp, acq, "bed-and-breakfast", out)
                if disp.remaining <= 0:
                    break

    # 3. Section 104 pool — interleave the remaining acquisitions,
    #    cost adjustments and disposals chronologically so a disposal only
    #    draws from the pool as it stood on its date. On a shared date the
    #    order is acquisition → adjustment → disposal.
    events: list[tuple[date, int, str, object]] = []
    events += [(a.date, 0, "acq", a) for a in acqs if a.remaining > 0]
    events += [(adj.date, 1, "adj", adj) for adj in (cost_adjustments or [])]
    events += [(d.date, 2, "disp", d) for d in disps if d.remaining > 0]
    events.sort(key=lambda e: (e[0], e[1]))

    pool_qty = Decimal(0)
    pool_cost = Decimal(0)
    for _, _order, kind, obj in events:
        if kind == "acq":  # acquisition → into the pool
            acq_lot = cast(_Lot, obj)
            pool_qty += acq_lot.remaining
            pool_cost += acq_lot.unit * acq_lot.remaining
            acq_lot.remaining = Decimal(0)
            continue
        if kind == "adj":  # ERI base-cost uplift on units still pooled
            if pool_qty > 0:
                pool_cost += cast(PoolCostAdjustment, obj).cost_gbp
            continue
        # disposal → draw from the pool at its average cost
        lot = cast(_Lot, obj)
        if pool_qty <= 0:
            # No pool to match against (incomplete history); record the
            # proceeds with zero cost so the gain isn't understated.
            out.append(
                MatchedDisposal(
                    disposal_date=lot.date,
                    disposal_qty=lot.remaining,
                    proceeds_gbp=_round(lot.unit * lot.remaining),
                    cost_gbp=Decimal("0.00"),
                    gain_gbp=_round(lot.unit * lot.remaining),
                    matched_against="s104",
                    acquisition_dates=[],
                )
            )
            lot.remaining = Decimal(0)
            continue
        qty = min(lot.remaining, pool_qty)
        avg = pool_cost / pool_qty
        cost = avg * qty
        proceeds = lot.unit * qty
        out.append(
            MatchedDisposal(
                disposal_date=lot.date,
                disposal_qty=qty,
                proceeds_gbp=_round(proceeds),
                cost_gbp=_round(cost),
                gain_gbp=_round(proceeds - cost),
                matched_against="s104",
                acquisition_dates=[],
            )
        )
        pool_qty -= qty
        pool_cost -= cost
        lot.remaining -= qty

    # Report disposals chronologically, and within a disposal in
    # rule-priority order (same-day, then 30-day, then pool).
    _bucket_order = {"same-day": 0, "bed-and-breakfast": 1, "s104": 2}
    out.sort(key=lambda m: (m.disposal_date, _bucket_order[m.matched_against]))
    return out
