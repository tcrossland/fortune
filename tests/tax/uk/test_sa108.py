"""SA108 capital-gains aggregation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.commodities_metadata import CommodityMetadata
from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.opening_positions import OpeningLot
from banking_pipeline.tax.uk.eri import EriEntry, cumulative_base_cost_adjustments
from banking_pipeline.tax.uk.sa108 import compute_sa108
from banking_pipeline.tax.uk.section_104 import PoolCostAdjustment


def _tx(
    *,
    doc_type: DocumentType,
    isin: str,
    qty: Decimal,
    amount: Decimal,
    on: date,
    currency: str = "GBP",
    gbp_rate: Decimal | None = None,
    accrued: Decimal | None = None,
) -> Transaction:
    return Transaction(
        trade_date=on,
        narration="trade",
        currency=currency,
        amount=amount,
        isin=isin,
        quantity=qty,
        gbp_rate=gbp_rate,
        accrued_interest=accrued,
        document_type=doc_type,
        source_path=Path("t.pdf"),
    )


def _reporting(isin: str, name: str = "Fund") -> CommodityMetadata:
    return CommodityMetadata(
        isin=isin,
        name=name,
        domicile="IE",
        reporting_status="reporting",
        asset_class="equity-etf",
        first_acquired=date(2018, 1, 1),
    )


def _dds(isin: str) -> CommodityMetadata:
    return CommodityMetadata(
        isin=isin,
        name="Discounted bond",
        domicile="DE",
        reporting_status="uk-domestic",
        asset_class="bond",
        first_acquired=date(2018, 1, 1),
        deeply_discounted=True,
    )


def test_basic_pool_disposal_gain_in_year() -> None:
    isin = "IE00B3VWN518"
    txs = [
        _tx(doc_type=DocumentType.BUY_ETF, isin=isin, qty=Decimal("100"),
            amount=Decimal("-1000"), on=date(2024, 5, 1)),
        _tx(doc_type=DocumentType.SELL_ETF, isin=isin, qty=Decimal("-100"),
            amount=Decimal("1500"), on=date(2025, 6, 1)),
    ]
    report = compute_sa108(
        txs, tax_year_label="2025-26", commodities={isin: _reporting(isin)}
    )
    assert len(report.rows) == 1
    row = report.rows[0]
    assert row.reporting_status == "reporting"
    assert row.gain_gbp == Decimal("500.00")
    assert row.match_type == "s104"
    assert row.commodity_name == "Fund"


def test_s104_row_dated_to_pool_first_acquisition() -> None:
    isin = "IE00B3VWN518"
    txs = [
        _tx(doc_type=DocumentType.BUY_ETF, isin=isin, qty=Decimal("50"),
            amount=Decimal("-500"), on=date(2022, 3, 1)),
        _tx(doc_type=DocumentType.BUY_ETF, isin=isin, qty=Decimal("50"),
            amount=Decimal("-600"), on=date(2023, 7, 1)),
        _tx(doc_type=DocumentType.SELL_ETF, isin=isin, qty=Decimal("-100"),
            amount=Decimal("1500"), on=date(2025, 6, 1)),
    ]
    report = compute_sa108(
        txs, tax_year_label="2025-26", commodities={isin: _reporting(isin)}
    )
    row = report.rows[0]
    assert row.match_type == "s104"
    # The pool has no single acquisition date; report the earliest.
    assert row.acquisition_dates == [date(2022, 3, 1)]


def test_rate_change_date_buckets_disposals() -> None:
    isin = "IE00B3VWN518"
    txs = [
        _tx(doc_type=DocumentType.BUY_ETF, isin=isin, qty=Decimal("200"),
            amount=Decimal("-2000"), on=date(2023, 5, 1)),  # pool unit 10
        _tx(doc_type=DocumentType.SELL_ETF, isin=isin, qty=Decimal("-50"),
            amount=Decimal("800"), on=date(2024, 10, 1)),   # before 30 Oct
        _tx(doc_type=DocumentType.SELL_ETF, isin=isin, qty=Decimal("-50"),
            amount=Decimal("900"), on=date(2024, 11, 1)),   # on/after
    ]
    report = compute_sa108(
        txs,
        tax_year_label="2024-25",
        commodities={isin: _reporting(isin)},
        rate_change_date=date(2024, 10, 30),
    )
    period = {r.disposal_date: r.period for r in report.rows}
    assert period[date(2024, 10, 1)] == "pre"
    assert period[date(2024, 11, 1)] == "post"


def test_no_rate_change_date_leaves_period_empty() -> None:
    isin = "IE00B3VWN518"
    txs = [
        _tx(doc_type=DocumentType.BUY_ETF, isin=isin, qty=Decimal("100"),
            amount=Decimal("-1000"), on=date(2023, 5, 1)),
        _tx(doc_type=DocumentType.SELL_ETF, isin=isin, qty=Decimal("-100"),
            amount=Decimal("1500"), on=date(2025, 6, 1)),
    ]
    report = compute_sa108(
        txs, tax_year_label="2025-26", commodities={isin: _reporting(isin)}
    )
    assert report.rows[0].period == ""


def test_opening_position_seeds_pool() -> None:
    isin = "IE00B3VWN518"
    # No ledger buy — only an opening lot (100 @ £800, unit 8) — then a sell.
    txs = [
        _tx(doc_type=DocumentType.SELL_ETF, isin=isin, qty=Decimal("-100"),
            amount=Decimal("1500"), on=date(2025, 6, 1)),
    ]
    opening = {
        isin: [
            OpeningLot(
                isin=isin, acquired=date(2019, 1, 1),
                quantity=Decimal("100"), cost_gbp=Decimal("800"),
            )
        ]
    }
    report = compute_sa108(
        txs, tax_year_label="2025-26", commodities={isin: _reporting(isin)},
        opening_positions=opening,
    )
    assert report.unmatched_isins == []
    assert report.rows[0].cost_gbp == Decimal("800.00")
    assert report.rows[0].gain_gbp == Decimal("700.00")


def test_disposal_without_acquisition_is_flagged() -> None:
    isin = "IE00B3VWN518"
    txs = [
        _tx(doc_type=DocumentType.SELL_ETF, isin=isin, qty=Decimal("-100"),
            amount=Decimal("1500"), on=date(2025, 6, 1)),
    ]
    report = compute_sa108(
        txs, tax_year_label="2025-26", commodities={isin: _reporting(isin)}
    )
    # Disposed with nothing acquired → flagged, and matched at zero cost.
    assert report.unmatched_isins == [isin]
    assert report.rows[0].cost_gbp == Decimal("0.00")
    assert report.rows[0].gain_gbp == Decimal("1500.00")


def test_deeply_discounted_routed_out_of_cgt() -> None:
    isin = "DE000BU3Z005"
    txs = [
        _tx(doc_type=DocumentType.BUY_BONDS, isin=isin, qty=Decimal("1000"),
            amount=Decimal("-900"), on=date(2024, 5, 1)),
        _tx(doc_type=DocumentType.SELL_BONDS, isin=isin, qty=Decimal("-1000"),
            amount=Decimal("1100"), on=date(2025, 6, 1)),
    ]
    report = compute_sa108(
        txs, tax_year_label="2025-26", commodities={isin: _dds(isin)}
    )
    # Taxed as income → not on the CGT rows, on dds_disposals instead.
    assert report.rows == []
    assert len(report.dds_disposals) == 1
    assert report.dds_disposals[0].gain_gbp == Decimal("200.00")


def test_undiscounted_bond_stays_in_cgt() -> None:
    isin = "DE000BU3Z005"
    txs = [
        _tx(doc_type=DocumentType.BUY_BONDS, isin=isin, qty=Decimal("1000"),
            amount=Decimal("-900"), on=date(2024, 5, 1)),
        _tx(doc_type=DocumentType.SELL_BONDS, isin=isin, qty=Decimal("-1000"),
            amount=Decimal("1100"), on=date(2025, 6, 1)),
    ]
    report = compute_sa108(
        txs, tax_year_label="2025-26", commodities={isin: _reporting(isin)}
    )
    assert len(report.rows) == 1
    assert report.dds_disposals == []


def test_eri_base_cost_uplift_reduces_gain() -> None:
    isin = "IE00B3VWN518"
    # Buy 1000 @ £1000; an ERI uplift of £400 before the sell raises the
    # pool cost to £1400, so the £1600 disposal gains £200 (not £600).
    txs = [
        _tx(doc_type=DocumentType.BUY_ETF, isin=isin, qty=Decimal("1000"),
            amount=Decimal("-1000"), on=date(2024, 1, 1)),
        _tx(doc_type=DocumentType.SELL_ETF, isin=isin, qty=Decimal("-1000"),
            amount=Decimal("1600"), on=date(2025, 2, 1)),
    ]
    adjustments = {
        isin: [PoolCostAdjustment(date(2024, 12, 30), Decimal("400"))]
    }
    report = compute_sa108(
        txs, tax_year_label="2024-25", commodities={isin: _reporting(isin)},
        cost_adjustments=adjustments,
    )
    assert report.rows[0].cost_gbp == Decimal("1400.00")
    assert report.rows[0].gain_gbp == Decimal("200.00")


def test_fig_relieved_eri_uplift_dropped_raises_later_disposal_gain() -> None:
    # End-to-end (ERI → SA108): a foreign reporting fund accrues ERI in a
    # FIG-claimed year (2025-26) and is disposed the next, taxable year
    # (2026-27). Claiming the ERI year suppresses its base-cost uplift (never
    # charged → no reg 99 uplift), so the disposal gain is higher than if the
    # uplift stood — the correction's whole point on a post-relief disposal.
    isin = "IE00B3VWN518"
    txs = [
        _tx(doc_type=DocumentType.BUY_ETF, isin=isin, qty=Decimal("1000"),
            amount=Decimal("-1000"), on=date(2023, 1, 10)),
        _tx(doc_type=DocumentType.SELL_ETF, isin=isin, qty=Decimal("-1000"),
            amount=Decimal("1600"), on=date(2026, 5, 1)),
    ]
    commodities = {isin: _reporting(isin)}  # IE / reporting → foreign situs
    entries = {isin: [EriEntry(
        isin=isin, period_end=date(2025, 6, 30),
        fund_distribution_date=date(2025, 12, 30),  # 2025-26
        income_type="interest", eri_per_unit=Decimal("0.50"),
        equalisation_per_unit=Decimal("0.10"), currency="GBP",
    )]}

    # No claim → £400 uplift applies → cost £1,400 → gain £200.
    adj_open, _ = cumulative_base_cost_adjustments(
        txs, eri_entries=entries, commodities=commodities,
    )
    open_year = compute_sa108(
        txs, tax_year_label="2026-27", commodities=commodities,
        cost_adjustments=adj_open,
    )
    assert open_year.rows[0].cost_gbp == Decimal("1400.00")
    assert open_year.rows[0].gain_gbp == Decimal("200.00")

    # Claim 2025-26 → the foreign uplift is suppressed → cost £1,000 → gain
    # £600; the delta is exactly the dropped uplift.
    adj_claim, _ = cumulative_base_cost_adjustments(
        txs, eri_entries=entries, commodities=commodities,
        fig_claim_years=frozenset({"2025-26"}),
    )
    claimed = compute_sa108(
        txs, tax_year_label="2026-27", commodities=commodities,
        cost_adjustments=adj_claim,
    )
    assert claimed.rows[0].cost_gbp == Decimal("1000.00")
    assert claimed.rows[0].gain_gbp == Decimal("600.00")
    assert (
        claimed.rows[0].gain_gbp - open_year.rows[0].gain_gbp == Decimal("400.00")
    )


def test_disposal_outside_year_excluded() -> None:
    isin = "IE00B3VWN518"
    txs = [
        _tx(doc_type=DocumentType.BUY_ETF, isin=isin, qty=Decimal("100"),
            amount=Decimal("-1000"), on=date(2023, 5, 1)),
        # Disposed in 2024-25, not 2025-26.
        _tx(doc_type=DocumentType.SELL_ETF, isin=isin, qty=Decimal("-100"),
            amount=Decimal("1500"), on=date(2024, 6, 1)),
    ]
    report = compute_sa108(
        txs, tax_year_label="2025-26", commodities={isin: _reporting(isin)}
    )
    assert report.rows == []


def test_unknown_metadata_tagged_unknown() -> None:
    isin = "US0378331005"
    txs = [
        _tx(doc_type=DocumentType.BUY_SHARES, isin=isin, qty=Decimal("50"),
            amount=Decimal("-500"), on=date(2024, 1, 1)),
        _tx(doc_type=DocumentType.SELL_ETF, isin=isin, qty=Decimal("-50"),
            amount=Decimal("400"), on=date(2025, 7, 1)),
    ]
    report = compute_sa108(txs, tax_year_label="2025-26", commodities={})
    assert len(report.rows) == 1
    assert report.rows[0].reporting_status == "unknown"
    assert report.rows[0].gain_gbp == Decimal("-100.00")  # a loss


def test_accrued_interest_excluded_from_consideration() -> None:
    isin = "DE000BU3Z005"
    txs = [
        # Net cash -1100 includes -100 accrued → capital cost is 1000.
        _tx(doc_type=DocumentType.BUY_BONDS, isin=isin, qty=Decimal("1000"),
            amount=Decimal("-1100"), on=date(2024, 5, 1), accrued=Decimal("-100")),
        # Net cash 1600 includes 100 accrued → capital proceeds are 1500.
        _tx(doc_type=DocumentType.SELL_BONDS, isin=isin, qty=Decimal("-1000"),
            amount=Decimal("1600"), on=date(2025, 6, 1), accrued=Decimal("100")),
    ]
    report = compute_sa108(
        txs, tax_year_label="2025-26", commodities={isin: _reporting(isin)}
    )
    assert report.rows[0].cost_gbp == Decimal("1000.00")
    assert report.rows[0].proceeds_gbp == Decimal("1500.00")
    assert report.rows[0].gain_gbp == Decimal("500.00")


def test_foreign_currency_uses_gbp_rate() -> None:
    isin = "US0378331005"
    txs = [
        _tx(doc_type=DocumentType.BUY_SHARES, isin=isin, qty=Decimal("100"),
            amount=Decimal("-1000"), on=date(2024, 5, 1),
            currency="USD", gbp_rate=Decimal("0.80")),  # cost 800 GBP
        _tx(doc_type=DocumentType.SELL_ETF, isin=isin, qty=Decimal("-100"),
            amount=Decimal("1500"), on=date(2025, 6, 1),
            currency="USD", gbp_rate=Decimal("0.90")),  # proceeds 1350 GBP
    ]
    report = compute_sa108(txs, tax_year_label="2025-26", commodities={})
    assert report.rows[0].cost_gbp == Decimal("800.00")
    assert report.rows[0].proceeds_gbp == Decimal("1350.00")
    assert report.rows[0].gain_gbp == Decimal("550.00")


def test_missing_rate_excludes_isin() -> None:
    isin = "US0378331005"
    txs = [
        _tx(doc_type=DocumentType.BUY_SHARES, isin=isin, qty=Decimal("100"),
            amount=Decimal("-1000"), on=date(2024, 5, 1), currency="USD"),
        _tx(doc_type=DocumentType.SELL_ETF, isin=isin, qty=Decimal("-100"),
            amount=Decimal("1500"), on=date(2025, 6, 1), currency="USD"),
    ]
    report = compute_sa108(txs, tax_year_label="2025-26", commodities={})
    assert report.rows == []
    assert report.missing_rate_isins == [isin]
    # The gap names the currency + month to add to the HMRC CSV. The
    # matcher breaks on the first unconverted trade (the 2024-05 buy).
    assert len(report.missing_rates) == 1
    gap = report.missing_rates[0]
    assert (gap.isin, gap.currency, gap.month) == (isin, "USD", "2024-05")


def test_match_history_residual_pools_tracks_current_holdings() -> None:
    from banking_pipeline.tax.uk.sa108 import match_history

    held = "IE00B3VWN518"  # partially disposed → still held
    exited = "IE00B4L5Y983"  # fully disposed → zero pool
    txs = [
        _tx(doc_type=DocumentType.BUY_ETF, isin=held, qty=Decimal("100"),
            amount=Decimal("-1000"), on=date(2024, 5, 1)),  # unit 10
        _tx(doc_type=DocumentType.SELL_ETF, isin=held, qty=Decimal("-40"),
            amount=Decimal("600"), on=date(2025, 6, 1)),
        _tx(doc_type=DocumentType.BUY_ETF, isin=exited, qty=Decimal("50"),
            amount=Decimal("-500"), on=date(2024, 5, 1)),
        _tx(doc_type=DocumentType.SELL_ETF, isin=exited, qty=Decimal("-50"),
            amount=Decimal("800"), on=date(2025, 6, 1)),
    ]
    history = match_history(
        txs,
        commodities={held: _reporting(held), exited: _reporting(exited)},
    )
    assert history.residual_pools[held].qty == Decimal("60")
    assert history.residual_pools[held].cost_gbp == Decimal("600")  # 60 @ avg 10
    assert history.residual_pools[exited].qty == Decimal("0")


def test_match_history_residual_pool_includes_deeply_discounted() -> None:
    # A held DDS security is routed out of the CGT rows but is still held —
    # the residual pool must cover it (a holdings view is tax-treatment blind).
    from banking_pipeline.tax.uk.sa108 import match_history

    isin = "US0378331005"
    txs = [
        _tx(doc_type=DocumentType.BUY_SHARES, isin=isin, qty=Decimal("100"),
            amount=Decimal("-1000"), on=date(2024, 5, 1)),
    ]
    history = match_history(txs, commodities={isin: _dds(isin)})
    assert history.rows == []
    assert history.residual_pools[isin].qty == Decimal("100")
    assert history.residual_pools[isin].cost_gbp == Decimal("1000")
