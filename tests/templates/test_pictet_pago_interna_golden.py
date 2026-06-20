"""Golden-file test for the Spanish-locale incoming self-to-self payment.

Pins the two-leg shape ``PAGO_INTERNA`` produces by routing through
``_render_third_party_payment``'s incoming-self-to-self branch:

  - Destination leg — Pictet portfolio credited with the cash in,
    ``Assets:<prefix>:<portfolio>:<ccy>`` signed positive.
  - Source leg — user's external account debited with the same
    amount, ``Equity:Transfers:<counter_account>:<ccy>`` signed
    negative (a perimeter crossing, not a holding).
  - Trailing ``no:`` reference comment if present.

This replaces the legacy ``_CASH_IN_TEMPLATE`` shape that posted to
``Equity:Uncategorized`` as an elastic counter-leg — ``Equity:Uncategorized``
was accumulating the entirety of the user's Revolut → Pictet wire
history, none of which was actually unclassified.

The fixture's date fields are anonymised to ``99.99.9999`` (which
:class:`datetime.date` rejects); the test substitutes ``30.06.2026`` to
keep the template parseable. The golden's ``2026-06-30`` reflects that
substitution.
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
from banking_pipeline.templates.pictet import PictetPagoInternaTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load_with_date(name: str, date_substitute: str) -> RawDocument:
    path = FIXTURES / name
    text = path.read_text(encoding="utf-8").replace("99.99.9999", date_substitute)
    return RawDocument(path=path, text=text, page_count=1)


def test_pago_interna_renders_to_golden_beancount() -> None:
    txs = PictetPagoInternaTemplate().extract(
        _load_with_date("pago_interna.txt", "30.06.2026")
    )
    assert len(txs) == 1, "Expected exactly one transaction from the fixture"

    classification = Classification(
        document_type=DocumentType.PAGO_INTERNA,
        confidence=0.95,
        source="rules",
        template_id="pictet.pago_interna.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.SPANISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (FIXTURES / "pago_interna.beancount").read_text(encoding="utf-8")

    assert rendered == golden, (
        "Rendered Pago Interna entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )
