"""Golden-file test for the 2025 ``Buy Shares`` fixture.

Distinct from ``test_pictet_buy_shares_golden`` (the NOVO NORDISK
DKK-denominated fixture) in two ways the 2025 fixture exercises:

  - **FX equity buy**: a SEK-denominated equity (FENIX OUTDOOR INTL.)
    bought from a GBP cash account, with the conversion priced inside
    the CASH EFFECT block (``Sub-total SEK -138'550.85`` →
    ``Net amount GBP -10'869.54``). The cash leg carries the
    ``@@ 138550.85 SEK`` annotation that records the FX leg.
  - **Multi-item fee breakdown on a buy**: the advice splits its
    Costs block into ``Forex spread SEK -1'778.05`` and
    ``Commission/Fee SEK -1'752.80``. The writer emits one
    ``Expenses:Pic:Fees:SEK`` posting per line item with the item's
    description as an inline beancount comment, mirroring the
    sells-with-breakdown shape (which uses a separate builder for
    posting-order reasons but the same per-item fee leg form).

The fees-breakdown path on buys was previously gated to sells only;
this golden pins the new buy-side behaviour so a future revert to a
single rolled-up fees leg surfaces here.
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
from banking_pipeline.templates.pictet import PictetBuySharesTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_buy_shares_2025_renders_to_golden_beancount() -> None:
    txs = PictetBuySharesTemplate().extract(_load("buy_shares.2025.txt"))
    assert len(txs) == 1, "Expected exactly one transaction from the fixture"

    classification = Classification(
        document_type=DocumentType.BUY_SHARES,
        confidence=0.95,
        source="rules",
        template_id="pictet.buy_shares.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.ENGLISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (FIXTURES / "buy_shares.2025.beancount").read_text(encoding="utf-8")

    assert rendered == golden, (
        "Rendered Buy Shares 2025 entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
