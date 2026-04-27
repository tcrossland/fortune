"""Document-type classifier facades.

Three facades live here:

* :class:`HybridClassifier` — single-stage rules-then-LLM classifier that
  evaluates every rule in ``DEFAULT_RULES``. Simplest possible pipeline.
* :class:`TwoStageClassifier` — classifies the bank first, then only evaluates
  that bank's rules plus the bank-agnostic ``GENERIC_RULES``. Kept as a named
  step for tests and for composition.
* :class:`LayeredClassifier` — the full three-stage default: language → bank
  → document type. This is the facade :class:`banking_pipeline.pipeline.Pipeline`
  instantiates by default.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from banking_pipeline.classifiers.bank import BankClassifier
from banking_pipeline.classifiers.language import LanguageClassifier
from banking_pipeline.classifiers.llm import LLMClassifier
from banking_pipeline.classifiers.rules import (
    GENERIC_RULES,
    RULESETS_BY_BANK,
    Rule,
    RuleClassifier,
)
from banking_pipeline.config import settings
from banking_pipeline.models import BankId, Classification, RawDocument


@dataclass
class HybridClassifier:
    """Single-stage rules → LLM classifier. Kept for direct use and testing."""

    rules: RuleClassifier = field(default_factory=RuleClassifier)
    llm: LLMClassifier = field(default_factory=LLMClassifier)
    threshold: float = settings.rule_confidence_threshold

    def classify(self, doc: RawDocument) -> Classification:
        rule_result = self.rules.classify(doc)
        if rule_result.confidence >= self.threshold:
            return rule_result

        # Rules weren't confident — try the LLM, but only if an API key is set.
        if not settings.anthropic_api_key:
            return rule_result

        llm_result = self.llm.classify(doc)
        return llm_result if llm_result.confidence > rule_result.confidence else rule_result


@dataclass
class TwoStageClassifier:
    """Classify the bank first, then run doc-type rules scoped to that bank.

    If the bank is ``UNKNOWN`` we fall back to ``GENERIC_RULES`` only (no
    per-bank rules), which prevents bank-specific phrases from false-matching
    on an unrelated issuer's documents.
    """

    bank_classifier: BankClassifier = field(default_factory=BankClassifier)
    llm: LLMClassifier = field(default_factory=LLMClassifier)
    generic_rules: tuple[Rule, ...] = GENERIC_RULES
    rulesets_by_bank: dict[BankId, tuple[Rule, ...]] = field(
        default_factory=lambda: dict(RULESETS_BY_BANK)
    )
    threshold: float = settings.rule_confidence_threshold

    def classify(self, doc: RawDocument) -> Classification:
        bank_result = self.bank_classifier.classify(doc)

        bank_specific = self.rulesets_by_bank.get(bank_result.bank, ())
        scoped_rules = bank_specific + self.generic_rules

        doc_type_result = RuleClassifier(rules=scoped_rules).classify(doc)

        if (
            doc_type_result.confidence < self.threshold
            and settings.anthropic_api_key
        ):
            llm_result = self.llm.classify(doc)
            if llm_result.confidence > doc_type_result.confidence:
                doc_type_result = llm_result

        # Attach the bank decision so downstream consumers (and the CLI) can
        # see both stages without an extra call.
        return doc_type_result.model_copy(update={"bank": bank_result})


@dataclass
class LayeredClassifier:
    """Three stages: language → bank → document type.

    Stages are independent — each stage's output is attached to the final
    ``Classification`` so callers can inspect every decision after the fact.
    Language detection currently informs routing and logging but does not yet
    gate which bank/doc-type rules run; once per-language rulesets land, the
    rule-selection step here is the obvious place to branch.
    """

    language_classifier: LanguageClassifier = field(default_factory=LanguageClassifier)
    two_stage: TwoStageClassifier = field(default_factory=TwoStageClassifier)

    def classify(self, doc: RawDocument) -> Classification:
        language_result = self.language_classifier.classify(doc)
        inner = self.two_stage.classify(doc)
        return inner.model_copy(update={"language": language_result})
