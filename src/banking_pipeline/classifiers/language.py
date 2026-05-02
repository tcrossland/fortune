"""Language identification — the outermost stage of the layered classifier.

We detect language *first* because every downstream stage (bank, document
type, field extraction) currently ships English-centric regexes. Knowing the
document language lets callers route Spanish statements to Spanish rulesets
(or to an LLM fallback) rather than silently misclassifying them.

Approach: a dependency-free stopword-frequency detector. For each supported
language we keep a small bag of high-frequency function words that are both
common in that language and unlikely to appear in the others. We count
occurrences in the document, pick the winner by absolute count, and shape a
confidence from (a) the margin over the runner-up and (b) the absolute count
of hits (so we don't become overconfident on a two-word input).

This is deliberately simple and stateless. For heavier workloads we'd swap
:class:`LanguageRuleClassifier` for ``langdetect`` or ``lingua`` — both MIT
and drop-in — but stopword counting is plenty for the banking-advice corpus
and adds zero dependencies.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field

from anthropic import Anthropic

from banking_pipeline.config import settings
from banking_pipeline.models import Language, LanguageClassification, RawDocument

# Stopword lists kept non-overlapping. Every entry below is either (a)
# exclusive to its language or (b) so overwhelmingly more common in that
# language that cross-hits don't distort the ranking. The lists mix two
# classes of tokens on purpose:
#
# * **Prose stopwords** (``the``, ``of``, ``que``, ``los`` …) — the classic
#   frequency-based language-ID signal. These dominate on any document with
#   real sentences.
# * **Banking-structural tokens** (``trade``, ``date``, ``fecha``,
#   ``cartera`` …) — section headers and field labels that recur across
#   Pictet's template family. Crucial for this corpus: the advices are
#   mostly ``Label: value`` rows with almost no prose, so the prose
#   stopwords alone score as few as 0–5 hits on an English fixture. The
#   domain tokens raise structured-doc counts to 30+ so the confidence
#   shape clears the LLM-fallback threshold without needing a network call.
#
# Notable omissions:
#   - ``de`` / ``en`` are *not* in the Spanish list even though they're
#     among the most frequent Spanish prose words. Every Pictet English
#     advice carries the French letterhead "Succursale **de** Luxembourg"
#     and beneficiary addresses containing "SUCURSAL **EN** ESPAN", which
#     caused ES to beat EN on short, prose-poor English fixtures.
#   - ``general`` is in neither list — Pictet uses it as a section header
#     in both languages, so it's not a disambiguator.
#   - ``bank`` / ``cash`` are deliberately excluded from English too: both
#     appear in Spanish fixtures (e.g. ``UBS(LUX)-SUSTAI.DEVEL.BANK``,
#     ``EFECTO CASH``).
ENGLISH_STOPWORDS: tuple[str, ...] = (
    # Prose stopwords.
    "the", "and", "of", "to", "in", "for", "with", "that", "this", "from",
    "is", "are", "was", "were", "be", "been", "have", "has", "had", "at",
    "on", "by", "as", "an", "it", "or", "not", "but", "if", "will",
    # Banking-structural English tokens — field labels / section headers
    # recurring across Pictet EN advices, each verified absent from every
    # Spanish fixture under ``tests/fixtures/es/``.
    "trade", "value", "booking", "date", "amount", "gross", "net",
    "account", "portfolio", "payment", "transaction", "client", "current",
    "publication", "order", "country", "costs", "quantity", "reference",
    "advice", "without", "signature", "information", "additional",
    "your", "yours", "faithfully", "ordinary", "incoming", "outgoing",
    "credit", "debit", "execution", "price",
)

SPANISH_STOPWORDS: tuple[str, ...] = (
    # Prose stopwords. ``de`` / ``en`` omitted — see module-level comment.
    "el", "la", "los", "las", "del", "y", "para", "con",
    "que", "este", "esta", "por", "son", "fue", "ser", "al", "un", "una",
    "su", "sus", "pero", "como", "más", "muy", "entre", "sobre", "cuando",
    "donde",
    # Banking-structural Spanish tokens — mirror of the English domain set,
    # drawn from Pictet's Spanish templates and checked against every
    # English fixture to avoid cross-hits.
    "fecha", "cuenta", "cartera", "importe", "operación", "ejecución",
    "bruto", "neto", "cliente", "cantidad", "bursátil", "corriente",
    "contable", "valor", "costes", "publicación", "atentamente", "aviso",
    "firma", "orden", "mercado", "depositario", "ordinario", "información",
    "adicional", "tipo", "transacción", "subtotal", "instrucción",
    "ordenante", "beneficiario", "país",
)


def _compile_stopwords(words: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    # Word-boundary match so we don't pick up "the" inside "then", and
    # case-insensitive because bank statements mix case freely.
    return tuple(re.compile(rf"\b{re.escape(w)}\b", re.I) for w in words)


@dataclass(frozen=True)
class LanguageRule:
    language: Language
    patterns: tuple[re.Pattern[str], ...]


LANGUAGE_RULES: tuple[LanguageRule, ...] = (
    LanguageRule(Language.ENGLISH, _compile_stopwords(ENGLISH_STOPWORDS)),
    LanguageRule(Language.SPANISH, _compile_stopwords(SPANISH_STOPWORDS)),
)


@dataclass
class LanguageRuleClassifier:
    """Count stopword occurrences per language, then shape a confidence.

    Confidence combines two signals:

    * **dominance**: ``(top - runner_up) / (top + runner_up + 1)`` — how
      decisively the top language beat the next one. The ``+1`` tempers
      small-sample blowouts (top=2, runner_up=0 shouldn't read as 1.0).
    * **evidence**: ``1 - exp(-top / 3)`` — saturating floor on how much
      absolute signal we had; a single stopword hit can't give >0.28.

    The final confidence is their product, kept in [0, 1] by construction.
    """

    rules: tuple[LanguageRule, ...] = field(default_factory=lambda: LANGUAGE_RULES)

    def classify(self, doc: RawDocument) -> LanguageClassification:
        counts: list[tuple[Language, int]] = []
        for rule in self.rules:
            total = sum(len(p.findall(doc.text)) for p in rule.patterns)
            counts.append((rule.language, total))

        # Sort descending; fall back to UNKNOWN when nothing matched at all.
        counts.sort(key=lambda pair: pair[1], reverse=True)
        top_lang, top_count = counts[0]
        runner_count = counts[1][1] if len(counts) > 1 else 0

        if top_count == 0:
            return LanguageClassification(
                language=Language.UNKNOWN, confidence=0.0, source="rules"
            )

        dominance = (top_count - runner_count) / (top_count + runner_count + 1)
        evidence = 1.0 - math.exp(-top_count / 3.0)
        confidence = dominance * evidence

        return LanguageClassification(
            language=top_lang, confidence=confidence, source="rules"
        )


_LLM_SYSTEM = f"""You identify the language a document is written in.
Return a single JSON object with keys:
  - language: an ISO 639-1 two-letter code, one of {[lang.value for lang in Language]}
  - confidence: float in [0, 1]
If the document is too short or mixed to decide, return language="unknown" and
a low confidence. Do not include any other text.
"""


@dataclass
class LanguageLLMClassifier:
    """LLM fallback, kept symmetric with :class:`BankLLMClassifier`."""

    model: str = settings.anthropic_model

    def classify(self, doc: RawDocument) -> LanguageClassification:
        client = Anthropic(api_key=settings.anthropic_api_key)
        # Language is decided from the first paragraph in practice.
        excerpt = doc.text[:1500]

        message = client.messages.create(
            model=self.model,
            max_tokens=128,
            system=_LLM_SYSTEM,
            messages=[{"role": "user", "content": excerpt}],
        )
        raw = "".join(block.text for block in message.content if block.type == "text")
        data = json.loads(raw)
        return LanguageClassification(
            language=Language(data["language"]),
            confidence=float(data["confidence"]),
            source="llm",
        )


@dataclass
class LanguageClassifier:
    """Rules first; fall back to an LLM only when rules are unsure and a key is set."""

    rules: LanguageRuleClassifier = field(default_factory=LanguageRuleClassifier)
    llm: LanguageLLMClassifier = field(default_factory=LanguageLLMClassifier)
    threshold: float = settings.rule_confidence_threshold

    def classify(self, doc: RawDocument) -> LanguageClassification:
        rule_result = self.rules.classify(doc)
        if rule_result.confidence >= self.threshold:
            return rule_result
        if not settings.anthropic_api_key:
            return rule_result
        llm_result = self.llm.classify(doc)
        return llm_result if llm_result.confidence > rule_result.confidence else rule_result
