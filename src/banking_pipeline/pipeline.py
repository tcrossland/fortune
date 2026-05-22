"""Top-level orchestration: PDF in, beancount text out."""

from __future__ import annotations

from pathlib import Path

import structlog

from banking_pipeline.classifiers import LayeredClassifier, TwoStageClassifier
from banking_pipeline.classifiers.hybrid import HybridClassifier
from banking_pipeline.extractors import load_pdf
from banking_pipeline.fields import HybridExtractor
from banking_pipeline.models import ExtractionResult

log = structlog.get_logger(__name__)

# Any classifier facade (single-stage, two-stage, or layered) satisfies the
# protocol the Pipeline needs: a ``classify(doc) -> Classification`` method.
Classifier = LayeredClassifier | TwoStageClassifier | HybridClassifier


class Pipeline:
    def __init__(
        self,
        classifier: Classifier | None = None,
        extractor: HybridExtractor | None = None,
    ) -> None:
        self.classifier = classifier or LayeredClassifier()
        self.extractor = extractor or HybridExtractor()

    def process(self, pdf_path: Path) -> ExtractionResult:
        log.info("loading", path=str(pdf_path))
        doc = load_pdf(pdf_path)

        classification = self.classifier.classify(doc)
        log.info(
            "classified",
            document_type=classification.document_type,
            confidence=classification.confidence,
            source=classification.source,
            bank=classification.bank.bank if classification.bank else None,
            bank_confidence=(
                classification.bank.confidence if classification.bank else None
            ),
            language=(
                classification.language.language if classification.language else None
            ),
            language_confidence=(
                classification.language.confidence if classification.language else None
            ),
        )

        transactions, warnings = self.extractor.extract(doc, classification)
        log.info("extracted", count=len(transactions), warnings=len(warnings))

        # Stamp the doctype onto each transaction so the JSONL sidecar
        # carries it (templates don't — they're per-doctype already).
        for tx in transactions:
            tx.document_type = classification.document_type

        return ExtractionResult(
            classification=classification,
            transactions=transactions,
            warnings=warnings,
            source_path=pdf_path,
        )
