"""Hybrid extractor: per-template regex first, LLM when not confident enough.

Failure mode worth knowing
--------------------------
When the classifier picks a template but the template returns ``[]``,
the old behaviour fell through to the generic regex extractor, which
in turn often produced a degraded ``Equity:Uncategorized``-balanced
placeholder entry that landed silently in the user's ledger. Three
distinct cases were getting flattened:

  1. **Template intentionally returns []** — the doctype is in
     :data:`~banking_pipeline.models.NO_OUTPUT_DOCTYPES` (statements,
     paired-advice openings, etc.). Empty result is correct.
  2. **No template was registered for the doctype** — falling through
     to regex is the right safety net.
  3. **Template was registered but returned [] anyway** — almost
     certainly a regression: the template's regexes drifted, the
     fixture changed, the layout changed. Falling through to regex
     papers over the bug with a junk entry.

The current dispatch handles all three correctly:

  - Case 1 returns ``[]`` immediately, logged at INFO.
  - Case 2 falls through to regex / LLM as before.
  - Case 3 returns ``[]`` immediately, logged at WARN — and raises
    :class:`TemplateExtractionError` when the extractor was
    constructed with ``strict=True`` so cron / CI / ``rebuild
    --strict`` notice. The regex / LLM fallback is *skipped* in this
    case so the regression surfaces as a missing entry rather than as
    a silently-degraded one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from banking_pipeline.config import settings
from banking_pipeline.fields.llm_extract import LLMExtractor
from banking_pipeline.fields.regex_extract import RegexExtractor
from banking_pipeline.models import (
    NO_OUTPUT_DOCTYPES,
    Classification,
    RawDocument,
    Transaction,
)
from banking_pipeline.templates import TEMPLATE_REGISTRY

_log = structlog.get_logger(__name__)


class TemplateExtractionError(Exception):
    """A template ran but returned no transactions for a doctype that
    should have produced at least one.

    Raised by :meth:`HybridExtractor.extract` only when ``strict=True``
    on the extractor; otherwise the same condition is logged at WARN
    and an empty transaction list is returned (with the regex/LLM
    fallback skipped, so the regression doesn't get masked by a
    placeholder ``Equity:Uncategorized`` entry).
    """

    def __init__(
        self,
        template_id: str,
        document_type: str,
        path: str,
    ) -> None:
        self.template_id = template_id
        self.document_type = document_type
        self.path = path
        super().__init__(
            f"Template {template_id} returned no transactions for "
            f"{document_type} document at {path}. Either the template "
            "is regressing (regex drift, layout change, fixture change) "
            "or the doctype should be added to "
            "models.NO_OUTPUT_DOCTYPES if it legitimately produces "
            "no output."
        )


@dataclass
class HybridExtractor:
    """Three-stage extractor: per-template → regex → LLM.

    Stages run in order, with the most reliable first. The per-template
    stage is the ground truth when it fires; the regex extractor is a
    safety net for unrecognised doctypes; the LLM is the final
    last-resort fallback for low-confidence regex output.

    ``strict`` controls how the extractor responds to a registered
    template returning ``[]`` for a doctype that should emit
    transactions (case 3 in the module docstring). When True, raises
    :class:`TemplateExtractionError`; when False, logs at WARN and
    returns an empty transaction list. The regex / LLM fallback is
    *skipped* in both modes — the goal is to surface the regression,
    not paper over it with a degraded placeholder.
    """

    regex: RegexExtractor = field(default_factory=RegexExtractor)
    llm: LLMExtractor = field(default_factory=LLMExtractor)
    threshold: float = settings.rule_confidence_threshold
    strict: bool = False

    def extract(
        self, doc: RawDocument, classification: Classification
    ) -> tuple[list[Transaction], list[str]]:
        warnings: list[str] = []
        doc_type = classification.document_type

        # 1. Prefer a per-template extractor if one is registered.
        if classification.template_id:
            template = TEMPLATE_REGISTRY.get(classification.template_id)
            if template is not None:
                txs = template.extract(doc)
                if txs:
                    return txs, warnings

                # Template ran but produced nothing. Distinguish
                # "expected empty" (doctype in NO_OUTPUT_DOCTYPES)
                # from "unexpected empty" (likely regression). In
                # both cases skip the regex/LLM fallback — falling
                # through historically produced
                # ``Equity:Uncategorized`` placeholder entries that
                # landed silently in the user's ledger.
                if doc_type in NO_OUTPUT_DOCTYPES:
                    _log.info(
                        "template_no_emit_doctype",
                        template_id=classification.template_id,
                        doc_type=doc_type.value,
                        path=str(doc.path),
                    )
                    return [], warnings

                _log.warning(
                    "template_extraction_empty",
                    template_id=classification.template_id,
                    doc_type=doc_type.value,
                    path=str(doc.path),
                    strict=self.strict,
                )
                if self.strict:
                    raise TemplateExtractionError(
                        template_id=classification.template_id,
                        document_type=doc_type.value,
                        path=str(doc.path),
                    )
                warnings.append(
                    f"Template {classification.template_id} returned no "
                    f"transactions for {doc_type.value}; regex/LLM "
                    "fallback skipped to avoid producing a degraded "
                    "placeholder. Set --strict to raise instead, or "
                    "investigate the template if this is a regression."
                )
                return [], warnings

        # 2. No template registered (or template_id unset) — fall back
        #    to the generic regex extractor as a safety net for
        #    unrecognised doctypes.
        txs, confidence = self.regex.extract(doc)
        if confidence >= self.threshold:
            return txs, warnings

        # 3. Final fallback: ask the LLM, but only if we have credentials.
        if not settings.anthropic_api_key:
            warnings.append(
                "Low-confidence regex extraction; set "
                "BANKPIPE_ANTHROPIC_API_KEY to enable LLM fallback."
            )
            return txs, warnings

        return self.llm.extract(doc, classification.document_type), warnings
