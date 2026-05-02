"""Golden-file test for the Spanish-locale outgoing payment.

Pins the third-party two-leg-elastic shape ``PAGO`` produces by
routing through ``_render_third_party_payment``:

  - Cash leg — Net amount, signed negative (cash out).
  - Elastic counter-leg — ``Expenses:<prefix>:<portfolio>:Other``
    catch-all (the destination ``BANCO SANTANDER`` doesn't resolve
    in the default ``counterparty_account_map``; populating that
    map would route the leg to the named expense account instead).
  - Trailing ``no:`` reference comment.

Self-to-self path (``BANCO SANTANDER`` → Revolut hypothetical) and
counterparty-mapped path (named beneficiary) are exercised by the
generic third-party-payment tests in ``test_counterparty_map.py``.
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
from banking_pipeline.templates.pictet import PictetPagoTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_pago_renders_to_golden_beancount() -> None:
    txs = PictetPagoTemplate().extract(_load("pago.txt"))
    assert len(txs) == 1, "Expected exactly one transaction from the fixture"

    classification = Classification(
        document_type=DocumentType.PAGO,
        confidence=0.95,
        source="rules",
        template_id="pictet.pago.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.SPANISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (FIXTURES / "pago.beancount").read_text(encoding="utf-8")

    assert rendered == golden, (
        "Rendered Pago entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
