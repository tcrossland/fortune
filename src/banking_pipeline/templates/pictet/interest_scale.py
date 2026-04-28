"""``pictet.interest_scale.v1`` — quarterly per-day interest rate ledger.

Pictet emits this document under ``CURRENT ACCOUNT / Interest scale`` as
the companion ledger to :mod:`interest_payment`: where the payment advice
carries the cash leg, the scale shows the per-day balance / rate
breakdown that produced it. **Both documents describe the same economic
event** for the same account and period.

To avoid double-counting, this template intentionally returns an empty
list — the matching ``INTEREST_PAYMENT`` advice is the source of truth
for the cash leg, and the scale's rate-by-bucket data isn't directly
representable in beancount postings anyway. The pipeline still
classifies and registers the document (so audit/diagnostic logs see it)
but no beancount entry is generated.

If the scale data ever becomes useful programmatically (e.g. for
verifying Pictet's rate calculations against the user's known reference
rates), a downstream tool can read ``CASH EFFECT``-block fields
directly without going through this extractor.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction


@dataclass
class PictetInterestScaleTemplate:
    template_id: str = "pictet.interest_scale.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        # Intentionally empty: the matching ``INTEREST_PAYMENT`` advice
        # carries the cash leg for this same period. Emitting anything
        # here would double-count when both documents are processed.
        return []
