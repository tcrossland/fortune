"""LLM-based structured extraction, gated to Pydantic schemas via Claude tool use."""

from __future__ import annotations

import json
from dataclasses import dataclass

from anthropic import Anthropic

from banking_pipeline.config import settings
from banking_pipeline.models import DocumentType, RawDocument, Transaction


@dataclass
class LLMExtractor:
    model: str = settings.anthropic_model

    def extract(self, doc: RawDocument, doc_type: DocumentType) -> list[Transaction]:
        client = Anthropic(api_key=settings.anthropic_api_key)

        tool_schema = {
            "name": "record_transactions",
            "description": "Record one or more transactions parsed from the document.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "transactions": {
                        "type": "array",
                        "items": Transaction.model_json_schema(),
                    }
                },
                "required": ["transactions"],
            },
        }

        # The SDK's typed overloads want TypedDict params for ``tools`` /
        # ``tool_choice`` / ``messages``; we pass plain dicts that are valid
        # at runtime. Narrowing them to the SDK's param types buys nothing
        # for this untested fallback path.
        message = client.messages.create(  # type: ignore[call-overload]
            model=self.model,
            max_tokens=2048,
            system=(
                "You extract accounting-relevant transactions from banking documents. "
                f"The document type is: {doc_type.value}. "
                "Return every transaction you find by calling the record_transactions tool."
            ),
            tools=[tool_schema],
            tool_choice={"type": "tool", "name": "record_transactions"},
            messages=[{"role": "user", "content": doc.text[:30_000]}],
        )

        for block in message.content:
            if block.type == "tool_use" and block.name == "record_transactions":
                payload = block.input
                # Anthropic may return dict or str depending on SDK version.
                if isinstance(payload, str):
                    payload = json.loads(payload)
                return [
                    Transaction(**{**t, "source_path": str(doc.path)})
                    for t in payload["transactions"]
                ]
        return []
