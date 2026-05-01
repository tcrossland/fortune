"""Golden-file test for the Pictet ``Spot`` FX-trade render.

Pins the two-leg shape that ``SPOT`` produces by routing through
``_render_internal_transfer`` (since both fixtures live under the
same intra-portfolio FX-bridge family):

  - Source-currency leg, signed negative (cash out).
  - Destination-currency leg, signed positive, with
    ``@@ <abs_source> <src_ccy>`` annotation that lets beancount
    cross-reconcile the two cash currencies on a single entry.
  - Trailing ``no:`` reference comment.

This replaces the legacy two-entry / ``Equity:Uncategorized``-balanced
shape the old ``_FX_LEG_TEMPLATE`` produced. A revert that splits the
single Transaction back into two would reintroduce the
``Equity:Uncategorized`` accumulation surfaced in the balance-sheet
review.
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
from banking_pipeline.templates.pictet import PictetSpotTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_spot_renders_to_golden_beancount() -> None:
    txs = PictetSpotTemplate().extract(_load("spot.txt"))
    assert len(txs) == 1, "Expected exactly one transaction from the fixture"

    classification = Classification(
        document_type=DocumentType.SPOT,
        confidence=0.95,
        source="rules",
        template_id="pictet.spot.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.ENGLISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (FIXTURES / "spot.beancount").read_text(encoding="utf-8")

    assert rendered == golden, (
        "Rendered Spot entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
