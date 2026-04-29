"""Golden-file test for the EN bond-purchase render.

Pins the four-leg shape ``BUY_BONDS`` produces, plus the inline
``open Assets:<prefix>:<portfolio>:<isin> <isin>`` directive emitted
on first-time security buys (``BUY_BONDS`` is in
``_OPEN_EMITTING_TYPES``):

  - Asset leg leads (the account receiving value), with the literal
    cost-basis brace ``{<unit_price> <currency>}``. The percentage
    price 97.512% is converted to per-face-unit 0.97512 EUR so
    beancount's inventory tracking sees a consistent EUR-denominated
    cost basis at acquisition.
  - Fees leg (Brokerage, expense).
  - Accrued-interest leg debited (positive amount on
    ``Income:<prefix>:<isin>:Interest`` because the buyer paid for
    accrued coupon belonging to the prior holder; the eventual
    coupon receipt will credit this account in full and the net
    income reflects the buyer's actual coupon entitlement).
  - Cash leg (Net amount, negative — the all-in debit posted to the
    portfolio's current account).

No ``:Realized`` leg — buys don't realise anything; the cost basis
is what the realised-gain calculation will draw on at sale time.
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
from banking_pipeline.templates.pictet import PictetBuyBondsTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_buy_bonds_renders_to_golden_beancount() -> None:
    txs = PictetBuyBondsTemplate().extract(_load("buy_bonds.txt"))
    assert len(txs) == 1, "Expected exactly one transaction from the fixture"

    classification = Classification(
        document_type=DocumentType.BUY_BONDS,
        confidence=0.95,
        source="rules",
        template_id="pictet.buy_bonds.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.ENGLISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (FIXTURES / "buy_bonds.beancount").read_text(encoding="utf-8")

    assert rendered == golden, (
        "Rendered Buy bonds entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
