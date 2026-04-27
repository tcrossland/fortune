"""LLM fallback classifier using Claude via the official Anthropic SDK."""

from __future__ import annotations

import json
from dataclasses import dataclass

from anthropic import Anthropic

from banking_pipeline.config import settings
from banking_pipeline.models import Classification, DocumentType, RawDocument

_SYSTEM = """You are an expert at classifying banking correspondence.
Return a single JSON object with keys:
  - document_type: one of {types}
  - confidence: float in [0, 1]
Do not include any other text.
""".format(types=[t.value for t in DocumentType])


@dataclass
class LLMClassifier:
    model: str = settings.anthropic_model

    def classify(self, doc: RawDocument) -> Classification:
        client = Anthropic(api_key=settings.anthropic_api_key)
        # Truncate to keep costs bounded — the first ~4 KB is almost always enough
        # to identify the document type.
        excerpt = doc.text[:4000]

        message = client.messages.create(
            model=self.model,
            max_tokens=256,
            system=_SYSTEM,
            messages=[{"role": "user", "content": excerpt}],
        )
        # The Messages API returns a list of content blocks; we expect one text block.
        raw = "".join(block.text for block in message.content if block.type == "text")
        data = json.loads(raw)

        return Classification(
            document_type=DocumentType(data["document_type"]),
            confidence=float(data["confidence"]),
            source="llm",
            template_id=None,
        )
