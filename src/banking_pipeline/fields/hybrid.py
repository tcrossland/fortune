"""Hybrid extractor: per-template regex first, LLM when not confident enough."""

from __future__ import annotations

from dataclasses import dataclass, field

from banking_pipeline.config import settings
from banking_pipeline.fields.llm_extract import LLMExtractor
from banking_pipeline.fields.regex_extract import RegexExtractor
from banking_pipeline.models import Classification, RawDocument, Transaction
from banking_pipeline.templates import TEMPLATE_REGISTRY


@dataclass
class HybridExtractor:
    regex: RegexExtractor = field(default_factory=RegexExtractor)
    llm: LLMExtractor = field(default_factory=LLMExtractor)
    threshold: float = settings.rule_confidence_threshold

    def extract(
        self, doc: RawDocument, classification: Classification
    ) -> tuple[list[Transaction], list[str]]:
        warnings: list[str] = []

        # 1. Prefer a per-template extractor if one is registered.
        if classification.template_id:
            template = TEMPLATE_REGISTRY.get(classification.template_id)
            if template is not None:
                txs = template.extract(doc)
                if txs:
                    return txs, warnings
                warnings.append(f"Template {classification.template_id} matched zero transactions.")

        # 2. Fall back to the generic regex extractor.
        txs, confidence = self.regex.extract(doc)
        if confidence >= self.threshold:
            return txs, warnings

        # 3. Final fallback: ask the LLM, but only if we have credentials.
        if not settings.anthropic_api_key:
            warnings.append("Low-confidence regex extraction; set BANKPIPE_ANTHROPIC_API_KEY to enable LLM fallback.")
            return txs, warnings

        return self.llm.extract(doc, classification.document_type), warnings
