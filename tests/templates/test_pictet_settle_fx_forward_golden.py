"""Golden-file test for the ``Settle FX forward`` render.

Pictet ``Settle FX forward`` advices record the physical settlement of
an OTC FX forward at maturity as two ``CASH EFFECT`` blocks (one per
leg) plus an upper ``Costs`` block carrying the ``Forward spread``
line. The render shape emits a single beancount entry with three
postings: the fee-bearing cash leg, the forward-spread expense leg,
and the counter cash leg with an ``@@ <abs_gross> <ccy>`` annotation
linking the two cash currencies.

Pins, against both fixtures (2026 buy-USD / 2025 sell-GBP):

  - Two-string narration: ``"Settle FX forward" "<headline>"``.
  - Fee-bearing cash leg first (signed as printed by Pictet — may be
    negative or positive depending on operation direction).
  - ``Expenses:<prefix>:Fees:<ccy>`` posting with the absolute-value
    spread and a ``; Forward spread`` comment.
  - Counter cash leg with ``@@ <abs(amount - fees)> <ccy>`` annotation
    — the @@ value is the pre-fee gross of the fee-bearing leg, which
    is what beancount needs to cross-reconcile the two cash currencies
    against the post-fee net cash impact.
  - Trailing ``no:`` reference comment.

Two fixtures cover both directions: ``settle_fx_forward.txt`` (2026,
Operation type Buy, fee on the cash-out GBP leg) and
``settle_fx_forward.2025.txt`` (2025, Operation type Sell, fee on the
cash-in EUR leg). Together they pin the writer's fee-leg detection
and the ``gross = amount - fees`` arithmetic in both signed-amount
regimes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
from banking_pipeline.templates.pictet import PictetSettleFxForwardTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def _classification() -> Classification:
    return Classification(
        document_type=DocumentType.SETTLE_FX_FORWARD,
        confidence=0.95,
        source="rules",
        template_id="pictet.settle_fx_forward.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.ENGLISH, confidence=0.99, source="rules"
        ),
    )


@pytest.mark.parametrize(
    "fixture, golden",
    [
        ("settle_fx_forward.txt", "settle_fx_forward.beancount"),
        ("settle_fx_forward.2025.txt", "settle_fx_forward.2025.beancount"),
    ],
)
def test_settle_fx_forward_renders_to_golden_beancount(
    fixture: str, golden: str
) -> None:
    txs = PictetSettleFxForwardTemplate().extract(_load(fixture))
    assert len(txs) == 1, "Expected exactly one transaction from the fixture"

    rendered = beancount_writer.render_entry(txs[0], _classification())
    expected = (FIXTURES / golden).read_text(encoding="utf-8")

    assert rendered == expected, (
        "Rendered Settle FX forward entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{expected}"
    )
