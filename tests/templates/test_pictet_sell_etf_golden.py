"""Golden-file test for the EN ETF-sale render.

Pins the four-leg shape ``SELL_ETF`` produces by routing through the
existing ``_render_security_trade`` builder via ``_SECURITY_SELL_TYPES``
membership:

  - Cash leg (Net amount, positive — proceeds in).
  - Fees leg (Commission/Fee, expense).
  - Asset leg (units leaving inventory at the trade price, with the
    empty-cost ``{}`` form so beancount reduces the position at its
    cost basis and ``@ <price>`` records the sale price for
    capital-gains attribution).
  - Elastic ``Income:<prefix>:<isin>:Realized`` leg.

Mirrors ``BUY_ETF``'s shape, sign-flipped and with the realised-gain
elastic leg added (sells need it, buys don't — buys carry an explicit
cost basis on the asset leg instead).
"""

from __future__ import annotations

from pathlib import Path

from banking_pipeline import beancount_writer
from banking_pipeline.models import (
    BankClassification,
    BankId,
    Classification,
    DocumentType,
    Language,
    LanguageClassification,
    RawDocument,
)
from banking_pipeline.templates.pictet import PictetSellEtfTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_sell_etf_renders_to_golden_beancount() -> None:
    txs = PictetSellEtfTemplate().extract(_load("sell_etf.txt"))
    assert len(txs) == 1, "Expected exactly one transaction from the fixture"

    classification = Classification(
        document_type=DocumentType.SELL_ETF,
        confidence=0.95,
        source="rules",
        template_id="pictet.sell_etf.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.ENGLISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (FIXTURES / "sell_etf.beancount").read_text(encoding="utf-8")

    assert rendered == golden, (
        "Rendered Sell ETF entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
