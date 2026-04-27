"""Domain models shared across pipeline stages."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class DocumentType(StrEnum):
    # --- Fund / security trading legs ---
    REDEMPTION_NOTICE = "redemption_notice"
    SUBSCRIPTION_NOTICE = "subscription_notice"
    # Fund-switch legs. Pictet documents these as two separate advices — the
    # "salida" (outgoing) leg is semantically a redemption-in-context-of-switch,
    # and "entrada" (incoming) is the paired subscription. Values kept in the
    # issuer's own vocabulary so the filename/enum correspondence stays obvious.
    SWITCH_SALIDA = "switch_salida"
    SWITCH_ENTRADA = "switch_entrada"
    # Structured-products purchase (PWM certificates, notes, etc.). Distinct
    # from ``SUBSCRIPTION_NOTICE`` because Pictet issues it under the
    # ``SECURITY / Buy structured products`` banner with an ``Asset type
    # Structured products`` field that changes the downstream posting rules.
    BUY_STRUCTURED_PRODUCTS = "buy_structured_products"
    # ETF purchase. Pictet emits this under ``SECURITY / Buy Exchange Traded
    # Fund`` with ``Asset type Exchange Traded Fund``. Field layout matches the
    # fund-subscription / structured-products advice (single CASH EFFECT block,
    # ``Operation type Buy``); the asset-type banner is the only reliable
    # discriminator at the classifier layer.
    BUY_ETF = "buy_etf"
    # Bond/structured-product maturity payout. Shares the ``SECURITY EVENT``
    # banner with dividend notices but uses ``Redemption price`` + an implicit
    # paired ``OUT of portfolio`` leg instead of an explicit sale execution.
    FINAL_REDEMPTION = "final_redemption"
    ACCOUNT_STATEMENT = "account_statement"
    TRADE_CONFIRMATION = "trade_confirmation"

    # --- FX ---
    # OTC derivatives. ``FX_FORWARD`` covers the vanilla physical-settlement
    # forward advices Pictet issues ("Buy CCY1 - Sell CCY2 at <forward rate>").
    # Non-deliverable forwards, options, swaps etc. will want their own values
    # when fixtures land; keep them distinct rather than one catch-all.
    FX_FORWARD = "fx_forward"
    # Maturity advice paired with ``FX_FORWARD`` — the booking that actually
    # settles the forward. Shares most of FX_FORWARD's fields but carries the
    # ``Settle FX forward`` title and non-zero CASH EFFECT amounts.
    SETTLE_FX_FORWARD = "settle_fx_forward"
    SPOT = "spot"

    # --- Income events ---
    DIVIDEND_NOTICE = "dividend_notice"
    INTEREST_NOTICE = "interest_notice"
    # Quarterly interest booking on a current account (credit/debit balance
    # interest). ``INTEREST_SCALE`` is the companion ledger-style document
    # showing the per-bucket rates that produced that interest total.
    INTEREST_PAYMENT = "interest_payment"
    INTEREST_SCALE = "interest_scale"

    # --- Fees / invoices ---
    FEE_NOTICE = "fee_notice"
    # Pictet "Debit of fees" advice (administration flat fee + account
    # maintenance). ``FACTURA`` is the Spanish-branch equivalent issued as a
    # tax-compliant invoice document under the Madrid succursale.
    DEBIT_OF_FEES = "debit_of_fees"
    DEBITO_DE_GASTOS = "debito_de_gastos"
    FACTURA = "factura"

    # --- Payments / cash movements ---
    WIRE_CONFIRMATION = "wire_confirmation"
    # Outgoing third-party payment (with beneficiary + IBAN).
    PAYMENT = "payment"
    # Incoming payment from an external bank. Distinct because the other
    # party is the *instructing* party, not the beneficiary.
    INCOMING_PAYMENT = "incoming_payment"
    # Book transfer between the client's own current accounts (typically
    # across currencies, via an implicit FX leg). Distinct from PAYMENT
    # because there is no external beneficiary: both legs terminate in
    # portfolio-owned ``Current account`` entries and the advice carries an
    # explicit ``Exchange rate`` + ``Sub-total`` pair.
    INTERNAL_TRANSFER = "internal_transfer"
    # Spanish-locale incoming payment where the ordering party is the client
    # themselves — i.e. a self-to-self transfer from a client-owned external
    # account (e.g. Revolut → Pictet). Structurally this is just the Pictet
    # ES ``TRÁFICO DE PAGOS / PAGO ENTRANTE`` advice; what makes it *interna*
    # is that the ``Ordenante`` name matches the account holder. Regex can't
    # make that cross-reference, so the classifier's rule matches the generic
    # payment-advice shape and the downstream extractor is responsible for
    # confirming the self-to-self property before booking it differently
    # (Assets:Revolut → Assets:Pictet rather than Income:External → Assets:*).
    PAGO_INTERNA = "pago_interna"

    # --- Periodic financial statements ---
    # Monthly portfolio report ("Financial Statement", "As at <day> <month>
    # <year>"). Characterised by an ``Executive summary`` page, per-currency
    # ``Current account statement`` sections, and a ``Portfolio valuation``
    # that lists every holding. Issued in English from Luxembourg.
    MONTHLY_STATEMENT = "monthly_statement"
    # Quarterly portfolio report — shares the ``Financial Statement`` banner
    # with the monthly/annual variants but is the slimmest of the three: no
    # portfolio valuation, no benchmark-indices page, no ESG block. Instead it
    # opens with a ``Summary of current accounts`` and a regulatory-profile
    # page (``Client classification``, ``Client profile``, ``Risk appetite``,
    # ``Time horizon``) — the MiFID review Pictet repeats on quarterly cadence.
    QUARTERLY_STATEMENT = "quarterly_statement"
    # Annual portfolio report — shares the "Financial Statement" banner with
    # ``MONTHLY_STATEMENT`` but adds EU-mandated ESG / SFDR disclosures
    # (``EU Taxonomy``, ``Responsible Investing``, ``Article 8/9`` funds) and
    # a ``Summary of current accounts`` section the monthly version omits.
    ANNUAL_STATEMENT = "annual_statement"
    # Spanish-locale monthly statement ("ESTADO FINANCIERO EN EUR / AL <day>
    # <month> <year>"). Mirrors ``MONTHLY_STATEMENT`` in structure (portfolio
    # valuation, benchmark indices, quarterly/annual performance blocks).
    ESTADO_MENSUAL = "estado_mensual"
    # Spanish-locale quarterly statement ("ESTADO FINANCIERO DEL <start> AL
    # <end>"). Distinctive for its regulatory-profile block (``Clasificación
    # cliente``, ``Perfil cliente``, ``Apetito de riesgo``) and account
    # recap (``Recapitulación de las cuentas corrientes``) — no portfolio
    # valuation table, no benchmark indices.
    ESTADO_TRIMESTRAL = "estado_trimestral"
    # Spanish-locale annual statement. Structurally a dead ringer for
    # ``ESTADO_TRIMESTRAL`` — same TOC, same ``Recapitulación`` + regulatory
    # block, same disclaimer. The only reliable tell is the banner date range:
    # a full-year ``DEL <d> ENERO <y> AL <d> DICIEMBRE <y>``. That phrase is
    # what distinguishes the two rules; everything else matches both fixtures.
    ESTADO_ANUAL = "estado_anual"

    # --- Credit / limits ---
    # Extension of a lombard / current-account credit line.
    LIMIT_EXTENSION = "limit_extension"

    # --- Order reporting ---
    # Pre-trade "Order information report" — Pictet's pre-execution disclosure
    # document. Bundles a ``Your trade instruction`` section (the proposed
    # BUY/SELL legs with ISIN, quantity, indicative quote), an ``IMPACT ON
    # CASH`` block (disinvested/invested/expected result), and a ``Costs
    # simulation`` estimating entry, recurring, and exit costs over an
    # ``Investment Period``. Issued under the Pictet Madrid branch.
    ORDER_INFORMATION_REPORT = "order_information_report"

    # --- Spanish-locale fund trading legs ---
    # Stock purchase at a listed exchange ("BOLSA DE VALORES / Compra"),
    # distinct from ``SUSCRIPCION`` which is the fund-subscription equivalent.
    # Kept in the issuer's Spanish vocabulary for the same reason as
    # ``SWITCH_SALIDA`` / ``SWITCH_ENTRADA``.
    COMPRA = "compra"
    SUSCRIPCION = "suscripcion"
    REEMBOLSO = "reembolso"

    UNKNOWN = "unknown"


class BankId(StrEnum):
    """Issuing bank identity. ``UNKNOWN`` means no bank-specific ruleset matched;
    the pipeline falls back to the generic rules in that case."""

    PICTET = "pictet"
    UNKNOWN = "unknown"


class Language(StrEnum):
    """Document language. Values are ISO 639-1 two-letter codes — add more as
    new fixtures land (e.g. ``FRENCH = "fr"``, ``GERMAN = "de"``). ``UNKNOWN``
    is a non-ISO sentinel meaning the detector couldn't choose between
    candidates (very short text, mixed-language docs, etc.); kept as
    ``"unknown"`` for symmetry with :class:`BankId.UNKNOWN`."""

    ENGLISH = "en"
    SPANISH = "es"
    UNKNOWN = "unknown"


class BankClassification(BaseModel):
    bank: BankId
    confidence: float = Field(ge=0.0, le=1.0)
    source: str  # "rules" | "llm"


class LanguageClassification(BaseModel):
    language: Language
    confidence: float = Field(ge=0.0, le=1.0)
    source: str  # "rules" | "llm"


class RawDocument(BaseModel):
    """PDF loaded from disk together with its extracted plain text."""

    model_config = ConfigDict(frozen=True)

    path: Path
    text: str
    page_count: int


class Classification(BaseModel):
    document_type: DocumentType
    confidence: float = Field(ge=0.0, le=1.0)
    source: str  # "rules" | "llm"
    # Free-form hint about which bank/template matched, e.g. "pictet.redemption_notice.v1".
    template_id: str | None = None
    # Result of the first-stage bank classification that produced this document-type
    # decision. ``None`` means the single-stage classifier was used.
    bank: BankClassification | None = None
    # Detected document language. Populated by the layered classifier, kept
    # optional so single-stage classifiers (and older tests) remain valid.
    language: LanguageClassification | None = None


class Transaction(BaseModel):
    """A single economic event extracted from a document."""

    trade_date: date
    settlement_date: date | None = None
    narration: str
    currency: str  # ISO-4217 (e.g. "EUR", "USD")
    amount: Decimal
    # Securities fields (optional: only present for trade/dividend docs).
    isin: str | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    # Account identifiers (optional: IBAN, broker account, etc.).
    account_number: str | None = None
    # Where it came from — useful for diffing and audit.
    source_path: Path
    source_page: int | None = None


class ExtractionResult(BaseModel):
    classification: Classification
    transactions: list[Transaction]
    warnings: list[str] = Field(default_factory=list)
    # The PDF this result came from. Stored at result-level (not just on each
    # ``Transaction``) so the source is still available when extraction yields
    # zero transactions — useful for the ``; source:`` header comment in the
    # rendered beancount output and for audit/diagnostic logs.
    source_path: Path
