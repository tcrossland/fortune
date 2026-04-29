"""Golden-file test for the EN bond-sale render.

Pins the five-leg shape unique to ``SELL_BONDS``:

  - Cash leg (Net amount, positive — proceeds in).
  - Fees leg (Commission/Fee, expense).
  - Accrued-interest leg (``Income:<prefix>:<isin>:Interest``, signed
    negative because income is credited).
  - Asset leg (face-value units leaving inventory at the
    percentage-derived per-unit price).
  - Elastic ``Income:<prefix>:<isin>:Realized`` leg.

The accrued-interest leg is the load-bearing distinction from a
regular fund/stock sell — bond buyers pay accrued interest on top of
the percentage-priced principal, and recognising it on a dedicated
income account keeps coupon yield separate from realised capital
gain/loss on the principal.
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
from banking_pipeline.templates.pictet import PictetSellBondsTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_sell_bonds_renders_to_golden_beancount() -> None:
    txs = PictetSellBondsTemplate().extract(_load("sell_bonds.txt"))
    assert len(txs) == 1, "Expected exactly one transaction from the fixture"

    classification = Classification(
        document_type=DocumentType.SELL_BONDS,
        confidence=0.95,
        source="rules",
        template_id="pictet.sell_bonds.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.ENGLISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (FIXTURES / "sell_bonds.beancount").read_text(encoding="utf-8")

    assert rendered == golden, (
        "Rendered Sell bonds entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
