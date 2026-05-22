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

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

import structlog

from banking_pipeline.config import settings
from banking_pipeline.fields.llm_extract import LLMExtractor
from banking_pipeline.fields.regex_extract import RegexExtractor
from banking_pipeline.fx.gbp_rates import GbpRateSource, build_rate_source
from banking_pipeline.models import (
    NO_OUTPUT_DOCTYPES,
    Classification,
    RawDocument,
    Transaction,
)
from banking_pipeline.templates import TEMPLATE_REGISTRY

_log = structlog.get_logger(__name__)


def _load_commodity_domiciles() -> dict[str, str]:
    """Return ``{isin: domicile}`` from the configured commodity metadata.

    Empty when no metadata file is configured / present. Used to override
    the ISIN-prefix withholding-country guess with the user's
    hand-curated domicile (see
    :meth:`HybridExtractor._enrich_withholding_country`).
    """

    # Local import: ``commodities_metadata`` imports ``fields.validators``,
    # and importing it at module scope here would close a cycle through
    # ``fields/__init__``. By call time (extractor construction) the
    # package is fully initialised.
    from banking_pipeline.commodities_metadata import load_commodities

    path = settings.commodities_metadata_path
    if path is None or not path.is_file():
        return {}
    return {isin: meta.domicile for isin, meta in load_commodities(path).items()}


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
    rate_source: GbpRateSource = field(
        default_factory=lambda: build_rate_source(settings)
    )
    # ISIN → domicile, the authoritative withholding-country source.
    commodity_domiciles: Mapping[str, str] = field(
        default_factory=_load_commodity_domiciles
    )

    def _enrich(self, txs: list[Transaction]) -> None:
        """Post-extraction enrichment from configured external sources."""

        self._enrich_gbp_rates(txs)
        self._enrich_withholding_country(txs)

    def _enrich_gbp_rates(self, txs: list[Transaction]) -> None:
        """Stamp each transaction with its trade-date GBP rate.

        GBP-denominated cash legs are 1:1 by definition; everything else
        is looked up against :attr:`rate_source` at the trade date. A
        missing rate leaves ``gbp_rate`` as ``None`` — extraction never
        fails on it.
        """

        for tx in txs:
            if tx.currency.upper() == "GBP":
                tx.gbp_rate = Decimal("1")
                continue
            rate = self.rate_source.get_rate(tx.trade_date, tx.currency)
            if rate is not None:
                tx.gbp_rate = rate

    def _enrich_withholding_country(self, txs: list[Transaction]) -> None:
        """Override the ISIN-prefix withholding country with the security's
        curated domicile when the commodity metadata has one.

        Templates set ``withholding_country`` to the ISIN's 2-letter
        prefix — a reasonable default for a direct foreign equity, but
        wrong for Eurobonds (``XS`` isn't a country), ADRs (a ``US`` ISIN
        over a foreign issuer), and funds whose registration country
        isn't the withholding jurisdiction. ``data/commodities.toml``'s
        ``domicile`` is user-maintained and authoritative, so it wins
        when present. Only WHT-bearing transactions are touched; a
        missing entry leaves the ISIN-prefix default in place.
        """

        if not self.commodity_domiciles:
            return
        for tx in txs:
            if tx.withholding_tax is None or tx.isin is None:
                continue
            domicile = self.commodity_domiciles.get(tx.isin)
            if domicile:
                tx.withholding_country = domicile.upper()

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
                    self._enrich(txs)
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
            self._enrich(txs)
            return txs, warnings

        # 3. Final fallback: ask the LLM, but only if we have credentials.
        if not settings.anthropic_api_key:
            warnings.append(
                "Low-confidence regex extraction; set "
                "BANKPIPE_ANTHROPIC_API_KEY to enable LLM fallback."
            )
            self._enrich(txs)
            return txs, warnings

        llm_txs = self.llm.extract(doc, classification.document_type)
        self._enrich(llm_txs)
        return llm_txs, warnings
