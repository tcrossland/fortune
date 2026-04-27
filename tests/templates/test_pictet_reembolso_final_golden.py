"""Golden-file test for the Pictet ES structured-product final-redemption render.

Pins the sell-path render shape that this fixture validates:

  - Booking-date entry date and two-string narration (canonical title +
    ``Reembolso - <fund>`` security-event subject line).
  - Cash-leg first (proceeds in), asset-leg second with the
    sell-from-inventory ``{} @ <price> <ccy>`` cost-basis form.
  - Elastic ``Income:<prefix>:<ISIN>:Realized`` posting that beancount
    auto-balances against the difference between the cost basis pulled
    from inventory and the cash proceeds — the realised gain/loss on
    these units.
  - Trailing ``no:`` reference comment, no ``^`` link (final
    redemptions aren't paired the way switches are).

Same shape applies to ``REDEMPTION_NOTICE``, ``REEMBOLSO``, and
``FINAL_REDEMPTION`` (the other security-sell doctypes) — once those
get their own goldens, the writer should reproduce them with the same
structure.
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
from banking_pipeline.templates.pictet import PictetReembolsoFinalTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_reembolso_final_renders_to_golden_beancount() -> None:
    txs = PictetReembolsoFinalTemplate().extract(_load("reembolso_final.txt"))
    assert len(txs) == 1, "Expected exactly one transaction from the fixture"

    classification = Classification(
        document_type=DocumentType.REEMBOLSO_FINAL,
        confidence=0.95,
        source="rules",
        template_id="pictet.reembolso_final.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.SPANISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (FIXTURES / "reembolso_final.beancount").read_text(
        encoding="utf-8"
    )

    assert rendered == golden, (
        "Rendered Reembolso final entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
