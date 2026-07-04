"""UK section 104 cost-basis lens."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from banking_pipeline.basis_lens import BasisLens, HoldingBasis
from banking_pipeline.commodities_metadata import CommodityMetadata
from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.tax.uk.basis import UkSection104Lens


def _tx(
    *,
    doc_type: DocumentType,
    isin: str,
    qty: Decimal,
    amount: Decimal,
    on: date,
) -> Transaction:
    return Transaction(
        trade_date=on,
        narration="trade",
        currency="GBP",
        amount=amount,
        isin=isin,
        quantity=qty,
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


def test_lens_conforms_to_protocol_and_identity() -> None:
    lens: BasisLens = UkSection104Lens(transactions=[], commodities={})
    assert lens.name == "uk-s104"
    assert lens.currency == "GBP"
    assert lens.basis_for() == {}


def test_lens_reports_held_holding_with_gbp_cost() -> None:
    held = "IE00B3VWN518"
    exited = "IE00B4L5Y983"
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
    lens = UkSection104Lens(
        transactions=txs,
        commodities={held: _reporting(held), exited: _reporting(exited)},
    )
    basis = lens.basis_for()

    # Fully-disposed holding is omitted; only the still-held one appears.
    assert set(basis) == {held}
    assert basis[held] == HoldingBasis(
        isin=held,
        held_qty=Decimal("60"),
        cost_amount=Decimal("600"),  # 60 @ avg 10
        currency="GBP",
        market_value=None,
    )


def test_lens_applies_eri_cost_adjustment() -> None:
    from banking_pipeline.tax.uk.section_104 import PoolCostAdjustment

    isin = "IE00B3VWN518"
    txs = [
        _tx(doc_type=DocumentType.BUY_ETF, isin=isin, qty=Decimal("100"),
            amount=Decimal("-1000"), on=date(2024, 5, 1)),
    ]
    commodities = {isin: _reporting(isin)}
    plain = UkSection104Lens(transactions=txs, commodities=commodities).basis_for()
    lifted = UkSection104Lens(
        transactions=txs, commodities=commodities,
        cost_adjustments={isin: [PoolCostAdjustment(date(2024, 6, 1), Decimal("50"))]},
    ).basis_for()
    # The ERI base-cost uplift raises the pooled cost by £50, and the lens
    # decomposes that portion into ``cost_adjustment``.
    assert plain[isin].cost_amount == Decimal("1000")
    assert plain[isin].cost_adjustment == Decimal("0")
    assert lifted[isin].cost_amount == Decimal("1050")
    assert lifted[isin].cost_adjustment == Decimal("50")


def test_lens_eri_uplift_reduced_proportionally_by_later_disposal() -> None:
    from banking_pipeline.tax.uk.section_104 import PoolCostAdjustment

    isin = "IE00B3VWN518"
    txs = [
        _tx(doc_type=DocumentType.BUY_ETF, isin=isin, qty=Decimal("100"),
            amount=Decimal("-1000"), on=date(2024, 5, 1)),  # avg 10
        _tx(doc_type=DocumentType.SELL_ETF, isin=isin, qty=Decimal("-50"),
            amount=Decimal("700"), on=date(2025, 6, 1)),  # after the uplift
    ]
    # ERI +£50 on 2024-06-01 lifts cost to 1050 (avg 10.50); the later sale of
    # 50 units removes 525, so half the ERI leaves with it. The decomposition
    # must be the ERI *remaining* in the residual pool (25), not the raw £50 —
    # this is the mechanism behind the real PICTET-EM figures.
    lifted = UkSection104Lens(
        transactions=txs, commodities={isin: _reporting(isin)},
        cost_adjustments={isin: [PoolCostAdjustment(date(2024, 6, 1), Decimal("50"))]},
    ).basis_for()
    assert lifted[isin].cost_amount == Decimal("525.00")
    assert lifted[isin].cost_adjustment == Decimal("25.00")


def test_lens_includes_deeply_discounted_holding() -> None:
    isin = "US0378331005"
    txs = [
        _tx(doc_type=DocumentType.BUY_SHARES, isin=isin, qty=Decimal("100"),
            amount=Decimal("-1000"), on=date(2024, 5, 1)),
    ]
    basis = UkSection104Lens(
        transactions=txs, commodities={isin: _dds(isin)}
    ).basis_for()
    assert basis[isin].held_qty == Decimal("100")
    assert basis[isin].cost_amount == Decimal("1000")
