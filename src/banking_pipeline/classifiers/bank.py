"""Bank identification — stage one of the two-stage classifier.

The bank is usually much easier to identify than the document type: letterhead,
legal entity name in the footer, SWIFT BIC, or a distinctive address are stable
signals that rarely collide across banks. Each bank gets a bag of regex
patterns; the highest-scoring bank wins.

Scoring intentionally counts *independent hits* rather than the fraction of
patterns matched. The fraction approach punishes richer rulesets: a bank with
Geneva-only, Luxembourg-only, and Madrid-only letterhead patterns can never
match more than a third of its own rules on any single document, so it never
clears the confidence threshold even with overwhelming evidence. Counting
saturates cleanly instead — three strong independent signatures is enough to
be confident, and additional hits push confidence asymptotically to 1. With
``k=1`` in ``1 - exp(-weight * hits)`` the curve is: 1 hit → 0.63, 2 → 0.87,
3 → 0.95 — so any two independent signatures already beat the default LLM
fallback threshold.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field

from anthropic import Anthropic

from banking_pipeline.config import settings
from banking_pipeline.models import BankClassification, BankId, RawDocument


@dataclass(frozen=True)
class BankRule:
    bank: BankId
    patterns: tuple[re.Pattern[str], ...]
    weight: float = 1.0


# Pictet identification. We cast a wide net intentionally: the patterns
# partition into mutually-exclusive categories (Geneva vs. Luxembourg vs.
# Madrid branch; EN-locale vs. ES-locale structural quirks) so no single
# document could ever match all of them. The hit-count scoring in
# :class:`BankRuleClassifier` expects this — every extra pattern is another
# chance to pick up an independent signature, not an additional denominator.
#
# The groups below are purposely ordered name → address → domain/reference
# → structural → account-format so a quick scan of the signals that fired
# on a given fixture tells you *how* we identified it.
BANK_RULES: tuple[BankRule, ...] = (
    BankRule(
        bank=BankId.PICTET,
        patterns=(
            # --- Name / legal-entity identity -----------------------------
            re.compile(r"\bpictet\b", re.I),
            # Geneva letterhead says "Banque Pictet", Luxembourg says "Bank
            # Pictet"; Madrid factura drops the leading word entirely so this
            # pattern won't hit there but ``pictet_cie_europe`` will.
            re.compile(r"\b(?:banque|bank)\s+pictet\b", re.I),
            re.compile(r"\bPictet\s+&\s+Cie\s+\(Europe\)\b", re.I),
            # --- Geneva head office ---------------------------------------
            re.compile(r"\bPICTCHGG\b"),  # Pictet & Cie SA BIC
            re.compile(r"\broute\s+des\s+acacias\b", re.I),  # Geneva HQ
            # --- Luxembourg branch letterhead -----------------------------
            # Present on every Luxembourg-issued advice — including the
            # non-security docs (interest_scale, debit_of_fees, payment,
            # limit_extension) that have no portfolio section and no
            # Telekurs references to match against.
            re.compile(r"\bSuccursale\s+de\s+Luxembourg\b", re.I),
            re.compile(r"\b15A,?\s+avenue\s+J\.F\.\s+Kennedy\b", re.I),
            # --- Madrid branch letterhead (Spanish invoices / factura) ---
            re.compile(r"\bSucursal\s+en\s+Espa[ñn]a\b", re.I),
            re.compile(r"\bJos[eé]\s+Ortega\s+y\s+Gasset\b", re.I),
            # --- Domains + Pictet-internal reference prefix --------------
            # ``0A08`` is the reference code Pictet prints on the first line
            # of every template. Unambiguous signal when present.
            re.compile(r"\b(?:pictet\.com|grupo\.pictet)\b", re.I),
            re.compile(r"\b0A08\b"),
            # --- English-locale structural quirks -------------------------
            re.compile(r"CASH\s*EFFECT\s*in\s+portfolio", re.I),
            re.compile(r"\bIBAN\?[A-Z]{2}\d"),
            re.compile(r"\bTelekurs\s+ID\b", re.I),
            # --- Spanish-locale structural quirks -------------------------
            re.compile(r"\bSALIDA\s*de\s+la\s+cartera\b", re.I),  # no-space "SALIDAde" also hits
            re.compile(r"\bENTRADA\s*en\s+la\s+cartera\b", re.I),  # paired leg, future-proofing
            re.compile(r"\bEFECTO\s+CASH\b", re.I),
            re.compile(r"\bN°\s*Telekurs\b", re.I),
            # --- Account format ------------------------------------------
            # Broadened from the original K-prefix-only regex: fixtures show
            # Pictet uses at least K- (portfolio accounts) and P- (cash/
            # payment accounts) as the alpha prefix, with the same
            # ``<letter>-NNNNNN.NNN`` shape. Accepting any uppercase letter
            # generalises without loosening the format check.
            re.compile(r"\b[A-Z]-\d{6}\.\d{3}\b"),
        ),
    ),
)


@dataclass
class BankRuleClassifier:
    rules: tuple[BankRule, ...] = field(default_factory=lambda: BANK_RULES)

    def classify(self, doc: RawDocument) -> BankClassification:
        best_bank = BankId.UNKNOWN
        best_hits = 0
        best_weight = 1.0

        for rule in self.rules:
            hits = sum(1 for p in rule.patterns if p.search(doc.text))
            if hits == 0:
                continue
            # Compare weighted hit counts so a higher-weight rule can overtake
            # a raw-hits-higher one — useful once we add more banks and need
            # to say "this signature is stronger per-hit than that one".
            if rule.weight * hits > best_weight * best_hits:
                best_hits = hits
                best_weight = rule.weight
                best_bank = rule.bank

        # Saturating confidence from independent hit count (see module
        # docstring for derivation). With weight=1.0: 2 hits → 0.87,
        # 3 hits → 0.95, 5+ hits → essentially 1.0.
        confidence = 1.0 - math.exp(-best_weight * best_hits)
        return BankClassification(bank=best_bank, confidence=confidence, source="rules")


_LLM_SYSTEM = f"""You identify which bank issued a piece of banking correspondence.
Return a single JSON object with keys:
  - bank: one of {[b.value for b in BankId]}
  - confidence: float in [0, 1]
If you can't tell, return bank="unknown" and a low confidence. Do not include any other text.
"""


@dataclass
class BankLLMClassifier:
    model: str = settings.anthropic_model

    def classify(self, doc: RawDocument) -> BankClassification:
        client = Anthropic(api_key=settings.anthropic_api_key)
        # Bank identity is almost always decided in the first page or two.
        excerpt = doc.text[:2000]

        message = client.messages.create(
            model=self.model,
            max_tokens=128,
            system=_LLM_SYSTEM,
            messages=[{"role": "user", "content": excerpt}],
        )
        raw = "".join(block.text for block in message.content if block.type == "text")
        data = json.loads(raw)
        return BankClassification(
            bank=BankId(data["bank"]),
            confidence=float(data["confidence"]),
            source="llm",
        )


@dataclass
class BankClassifier:
    """Rules first; fall back to an LLM when confidence is low and a key is set."""

    rules: BankRuleClassifier = field(default_factory=BankRuleClassifier)
    llm: BankLLMClassifier = field(default_factory=BankLLMClassifier)
    threshold: float = settings.rule_confidence_threshold

    def classify(self, doc: RawDocument) -> BankClassification:
        rule_result = self.rules.classify(doc)
        if rule_result.confidence >= self.threshold:
            return rule_result
        if not settings.anthropic_api_key:
            return rule_result
        llm_result = self.llm.classify(doc)
        return llm_result if llm_result.confidence > rule_result.confidence else rule_result
