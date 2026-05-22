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
    # Direct equity purchase. Pictet emits this under ``SECURITY / Buy Shares``
    # with ``Asset type Equities`` for direct equity holdings (single shares
    # listed on an exchange — Novo Nordisk, etc.). Same skeleton as
    # ``BUY_ETF`` / ``BUY_STRUCTURED_PRODUCTS`` (single CASH EFFECT block,
    # ``Operation type Buy``); the asset-type banner is the only reliable
    # classifier discriminator from the other ``Buy <type>`` variants.
    BUY_SHARES = "buy_shares"
    # Bond/structured-product maturity payout. Shares the ``SECURITY EVENT``
    # banner with dividend notices but uses ``Redemption price`` + an implicit
    # paired ``OUT of portfolio`` leg instead of an explicit sale execution.
    FINAL_REDEMPTION = "final_redemption"
    # Structured-product sale advice — Pictet emits this under
    # ``SECURITY / Sell structured products`` when a held PWM equity
    # certificate (PEC) or similar OTC structured product is sold back.
    # Field skeleton mirrors the buy advice (single CASH EFFECT,
    # ``Operation type Sell``, ``Asset type Structured products``);
    # the ISIN field can carry either a real ISIN (real Swiss/EU
    # certificates) or a Pictet-internal ``ZZ...`` code with the
    # PDF-extractor space artifact.
    SELL_STRUCTURED_PRODUCTS = "sell_structured_products"
    # ETF sale advice — Pictet emits this under ``SECURITY / Sell
    # Exchange Traded Fund`` when an ETF holding is unwound. Field
    # skeleton mirrors the buy advice (single CASH EFFECT,
    # ``Operation type Sell``, ``Asset type Exchange Traded Fund``);
    # quantity and price are unit-count and trade-currency, so the
    # shared trade-advice helper handles parsing.
    SELL_ETF = "sell_etf"
    # Bond sale advice — Pictet emits this under ``SECURITY / Sell bonds``
    # when a held bond is sold before maturity. Distinct from
    # ``REDEMPTION_NOTICE`` (fund redemption) and ``FINAL_REDEMPTION`` (bond
    # held to maturity) because: the units are face-value nominal
    # (``Executed nominal EUR -90'000.00``) not unit count, the price is
    # quoted as a percentage of face value (``102.902%``), and the
    # advice carries an ``Interest`` line in the CASH EFFECT block — accrued
    # interest paid by the buyer that's recognised separately from the
    # principal proceeds.
    SELL_BONDS = "sell_bonds"
    # Bond purchase advice — Pictet emits this under ``STOCK EXCHANGE /
    # Purchase`` when a bond is bought via the OTC desk. Mirrors
    # ``SELL_BONDS``: face-value nominal (positive on buy), percentage
    # price (e.g. ``97.512%``), and an ``Interest`` line in the CASH
    # EFFECT block — accrued interest the buyer pays to the seller for
    # the period since the last coupon. The Operation-type vocabulary
    # differs from the sell side (``Purchase`` vs ``Sell``); the
    # ``Executed nominal`` + percentage-priced ``Execution price``
    # combination is the load-bearing structural marker of a bond
    # advice in either direction.
    BUY_BONDS = "buy_bonds"
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
    # Spanish-locale FX advices issued by Pictet's Madrid branch under the
    # ``MERCADO DE DIVISAS`` banner. The three sub-types mirror the EN
    # ``SPOT`` / ``FX_FORWARD`` / ``SETTLE_FX_FORWARD`` triple:
    #
    #   - ``CAMBIO_DE_DIVISAS`` — spot FX exchange (``Cambio de divisas
    #     al contado``). Two ``EFECTO CASH`` legs in opposite signs;
    #     same render shape as ``SPOT`` (single Transaction with both
    #     legs, routed through the internal-transfer builder).
    #   - ``CAMBIO_DE_DIVISAS_APERTURA`` — forward opening
    #     (``Cambio de divisas a plazo (apertura)``). Both ``EFECTO
    #     CASH`` blocks carry zero — paper-trail only; the matching
    #     ``CAMBIO_DE_DIVISAS_CIERRE`` advice books the cash leg at
    #     maturity. No-emit (same precedent as ``FX_FORWARD``).
    #   - ``CAMBIO_DE_DIVISAS_CIERRE`` — forward settlement
    #     (``Cambio de divisas a plazo (cierre)``). Carries a ``Spread``
    #     fee in one leg's currency; same render shape as
    #     ``SETTLE_FX_FORWARD`` (fee-bearing leg + counter leg + spread
    #     posting), routed through the fx-settlement builder.
    CAMBIO_DE_DIVISAS = "cambio_de_divisas"
    CAMBIO_DE_DIVISAS_APERTURA = "cambio_de_divisas_apertura"
    CAMBIO_DE_DIVISAS_CIERRE = "cambio_de_divisas_cierre"

    # --- Income events ---
    DIVIDEND_NOTICE = "dividend_notice"
    # Quarterly interest booking on a current account (credit/debit balance
    # interest). ``INTEREST_SCALE`` is the companion ledger-style document
    # showing the per-bucket rates that produced that interest total —
    # the scale is informational only and doesn't generate a beancount
    # entry; the payment carries the cash leg.
    INTEREST_PAYMENT = "interest_payment"
    INTEREST_SCALE = "interest_scale"

    # --- Fees / invoices ---
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
    # account (e.g. Revolut → Pictet). Pictet prints the title in all caps
    # (``PAGO ENTRANTE``) on this variant; structurally it's the same as
    # the third-party variant below but bookkeeps to a different posting
    # shape (Assets:Revolut → Assets:Pictet rather than
    # Income:External → Assets:*). The classifier discriminates against
    # ``PAGO_ENTRANTE`` via the title's case-sensitivity.
    PAGO_INTERNA = "pago_interna"
    # Spanish-locale third-party incoming payment — payment from a real
    # external counterparty (e.g. an employer earnout, a vendor invoice
    # paid). Pictet prints the title as ``Pago entrante`` (mixed case),
    # vs ``PAGO ENTRANTE`` (all caps) for the self-to-self ``PAGO_INTERNA``
    # variant. Books to ``Income:Pic:Other`` (or whichever income account
    # the user routes the payer to) rather than to a self-owned external
    # asset account.
    PAGO_ENTRANTE = "pago_entrante"
    # Spanish-locale outgoing third-party payment — counterpart to the
    # English ``PAYMENT`` doctype. Pictet prints it under ``TRÁFICO DE
    # PAGOS / Pago`` (mixed-case ``Pago``, alone on its own line) with
    # a ``Beneficiario`` block carrying the destination details. Same
    # render shape as ``PAYMENT``: self-to-self three-leg form when
    # the destination bank resolves via ``beneficiary_bank_map``;
    # third-party two-leg-elastic form otherwise (with optional
    # counterparty-name routing via ``counterparty_account_map``).
    # Distinct from ``PAGO_ENTRANTE`` (mixed-case ``Pago entrante``,
    # incoming third-party) and ``PAGO_INTERNA`` (all-caps ``PAGO
    # ENTRANTE``, incoming self-to-self).
    PAGO = "pago"
    # Spanish-locale cross-currency book transfer between two of the
    # client's own current accounts at Pictet — counterpart to the EN
    # ``INTERNAL_TRANSFER``. Issued under ``TRÁFICO DE PAGOS /
    # TRANSFERENCIA INTERNA DE EFECTIVO`` with two ``EFECTO CASH``
    # legs (debit on the source-currency account, credit on the
    # destination-currency account) and an in-block ``Tipo de cambio``
    # FX rate. Renders through the same single-entry-with-``@@``
    # builder as ``INTERNAL_TRANSFER``.
    TRANSFERENCIA_INTERNA = "transferencia_interna"

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
    # Stock-exchange sale ("BOLSA DE VALORES / Venta") — sell counterpart
    # to ``COMPRA``. Distinguished from ``REEMBOLSO`` (fund redemption)
    # by the standalone ``Venta`` title and the stock-trading fee
    # breakdown (``Corretaje y/o spread`` + ``Tasa bursátil``) the
    # advice carries.
    VENTA = "venta"
    SUSCRIPCION = "suscripcion"
    REEMBOLSO = "reembolso"
    # Spanish-locale final redemption — Pictet's structured-product maturity
    # payout, analogous to the EN ``FINAL_REDEMPTION``. Issued under
    # ``HECHOS RELEVANTES / REEMBOLSO / Reembolso final`` (security event,
    # not a stock-exchange trade), so the document carries no
    # ``Tipo de operación`` / ``Plaza bursátil`` and uses ``Cantidad`` /
    # ``Precio de rembolso`` instead of the trade-advice labels.
    REEMBOLSO_FINAL = "reembolso_final"
    # Spanish-locale fund distribution / ordinary dividend — Pictet's
    # Madrid-branch counterpart to the EN ``DIVIDEND_NOTICE``. Issued under
    # ``HECHOS RELEVANTES / Distribución / Dividendo ordinario`` when a
    # held fund pays an income distribution. Field skeleton mirrors the
    # EN advice: ``Cantidad detenida`` (Quantity held) records the
    # underlying position and ``Renta unitaria`` (Income per unit)
    # records the per-share dividend; trade/value/booking dates align
    # with ex/payment/booking dates the same way.
    DISTRIBUCION = "distribucion"

    # --- Spanish-locale securities transfers (free-of-payment) ----------
    # Pictet's Madrid branch issues these under ``LIQUIDACIÓN`` when
    # securities move *into* a portfolio from another custodian without
    # a cash payment (the originating bank delivers the position; no
    # purchase happens at Pictet). Two paired sub-types cover the
    # arrival, mirroring the FX_FORWARD/SETTLE_FX_FORWARD pattern:
    #
    #   - ``LIQUIDACION_AVISO_PREVIO_RECEPCION`` — pre-arrival notice
    #     (``LIQUIDACIÓN / AVISO PREVIO - RECEPCIÓN DE VALORES``).
    #     Pictet announces upcoming transfers from an external bank;
    #     the document carries the ENTRADA blocks for the announced
    #     positions but the comment ``Un aviso seguirá a la recepción
    #     real de cada posición`` makes clear that this is informational
    #     only — the actual booking happens in the paired
    #     ``RECEPCION_DE_VALORES`` advice. No-emit (same precedent as
    #     ``FX_FORWARD`` / ``CAMBIO_DE_DIVISAS_APERTURA``).
    #
    #   - ``LIQUIDACION_RECEPCION_DE_VALORES`` — actual receipt
    #     (``LIQUIDACIÓN / RECEPCIÓN DE VALORES (GRATUITA)``). Books
    #     the position with cost basis at the transfer's market value
    #     (``Estimacion de transferencia EUR ...``). Renders through a
    #     dedicated transfer-in builder that emits an asset leg with
    #     total-cost ``{{<total> <ccy>, <lot_date>}}`` braces and an
    #     ``Equity:<prefix>:<portfolio>:Transfers`` offset leg — no
    #     cash leg, since the receipt is free of payment.
    LIQUIDACION_AVISO_PREVIO_RECEPCION = "liquidacion_aviso_previo_recepcion"
    LIQUIDACION_RECEPCION_DE_VALORES = "liquidacion_recepcion_de_valores"

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


# Doctypes that legitimately produce no transactions / no beancount
# output. Two callers consult this set:
#
#   - The **writer** short-circuits at the top of :func:`render` /
#     :func:`render_entry` — if the doctype is in this set, no header,
#     no postings, no warnings; the document is paper-trail-only.
#   - The **extractor** treats a per-template ``[]`` return as
#     *expected* when the doctype is in this set (template did the
#     right thing). For doctypes NOT in this set, an empty extraction
#     is treated as a regression — the regex/LLM fallback is skipped
#     so the broken template doesn't get papered over with an
#     ``Equity:Uncategorized`` placeholder, and the failure is logged
#     loudly (or raised, in strict mode).
#
# Two families:
#
#   - **Paired-advice openings**: ``FX_FORWARD`` /
#     ``CAMBIO_DE_DIVISAS_APERTURA`` /
#     ``LIQUIDACION_AVISO_PREVIO_RECEPCION`` — the opening of a
#     contract or a pre-arrival notice with zero cash effect; the
#     paired settlement / arrival advice is the canonical paper trail
#     for the cash exchange or position arrival.
#   - **Periodic valuation statements**: monthly / quarterly / annual
#     reports across both locales. These describe portfolio
#     valuations at a point in time, not transactions; their cash
#     events have already been booked by the per-trade and
#     per-cash-movement advices that fed them, so emitting any
#     postings would double-count.
NO_OUTPUT_DOCTYPES: frozenset["DocumentType"] = frozenset({
    DocumentType.FX_FORWARD,
    DocumentType.CAMBIO_DE_DIVISAS_APERTURA,
    DocumentType.LIQUIDACION_AVISO_PREVIO_RECEPCION,
    # Periodic-valuation statements (English / Pictet Luxembourg).
    DocumentType.MONTHLY_STATEMENT,
    DocumentType.QUARTERLY_STATEMENT,
    DocumentType.ANNUAL_STATEMENT,
    # Periodic-valuation statements (Spanish / Pictet Madrid).
    DocumentType.ESTADO_MENSUAL,
    DocumentType.ESTADO_TRIMESTRAL,
    DocumentType.ESTADO_ANUAL,
    # Generic non-bank-specific account statement.
    DocumentType.ACCOUNT_STATEMENT,
})


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


class FeeItem(BaseModel):
    """One line item from a multi-component fee advice.

    Pictet's quarterly fee advices (``Débito de gastos`` / ``Debit of fees``
    / ``Factura``) carry a ``Costes`` block with one line per fee component
    — management fees, account-maintenance fees, foreign VAT, etc. — plus
    a ``Total`` summary line. Each line maps to one ``FeeItem`` here, and
    the writer renders one ``Expenses:<prefix>:Fees:<ccy>`` posting per
    item with the item's description as an inline beancount comment so
    the audit detail isn't lost when the fees roll up into a single cash
    leg.

    ``amount`` is stored signed-as-printed (Pictet writes fees negative
    because they're cash-out); the writer flips to ``abs(amount)`` for
    the expense leg since beancount expense-account postings are positive.
    """

    description: str
    amount: Decimal
    currency: str  # ISO-4217


class Transaction(BaseModel):
    """A single economic event extracted from a document.

    Currency semantics
    ------------------
    ``currency`` is the **cash-leg currency** — the currency the client's
    current account is debited or credited in. ``security_currency`` is the
    **trade-execution currency** for the asset itself. On non-FX trades the
    two are equal; on FX trades (e.g. a EUR-denominated client account
    buying a USD-denominated fund) Pictet bills the gross + fees in the
    security currency, converts via an FX leg inside the same advice, and
    prints the converted ``net`` in the cash currency. The writer emits a
    beancount ``@@ <subtotal> <ccy>`` annotation on the cash leg to record
    that conversion.

    Most fields default to ``None`` so non-security documents (fees,
    interest, payments) and pre-FX-aware extractors continue to construct
    a valid ``Transaction`` without filling fields that don't apply.
    """

    # --- Dates ----------------------------------------------------------
    trade_date: date
    settlement_date: date | None = None
    # Pictet ES: ``Fecha contable`` / EN: ``Booking date``. The writer uses
    # this rather than ``trade_date`` for the entry-date posting on advices
    # that carry one — booking is when the cash actually moved.
    booking_date: date | None = None

    # --- Narration ------------------------------------------------------
    narration: str
    # Document title (``Suscripción``, ``Trade confirmation``, etc.).
    # Beancount entries can carry two narration strings (payee + narration);
    # the writer uses ``title`` as the first and ``narration`` as the
    # second when both are present.
    title: str | None = None

    # --- Cash leg -------------------------------------------------------
    currency: str  # ISO-4217 cash-account currency (e.g. "EUR", "USD")
    amount: Decimal  # signed, in ``currency``

    # --- Security leg ---------------------------------------------------
    isin: str | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    # The trade-execution currency for the asset. ``None`` for non-security
    # documents. When this differs from ``currency`` the document is an FX
    # trade and ``subtotal_security`` / ``fees`` should also be populated.
    security_currency: str | None = None

    # --- Fees -----------------------------------------------------------
    # Pictet bills fees in the security currency, not the cash currency, so
    # the explicit ``fees_currency`` is kept rather than assuming it equals
    # either ``currency`` or ``security_currency``.
    fees: Decimal | None = None
    fees_currency: str | None = None
    # Per-line breakdown of a multi-component fee advice's ``Costes`` block.
    # Empty for documents that don't carry one (every trade and switch
    # advice today rolls fees into a single ``fees`` line above; only
    # standalone fee advices like ``Débito de gastos`` populate this).
    # The writer iterates this when present and emits one expense leg per
    # item; otherwise it falls back to a single aggregate expense leg.
    fee_breakdown: list[FeeItem] = Field(default_factory=list)

    # --- FX bridge (only set when security_currency != currency) --------
    # Pre-FX subtotal in the security currency: gross + fees, before
    # conversion to the cash-account currency. Printed verbatim by Pictet
    # as ``Subtotal <ccy> <amount>``; preserved explicitly so the writer
    # doesn't have to re-derive it (and risk rounding drift on documents
    # that round at different stages).
    subtotal_security: Decimal | None = None
    # Documentation field — beancount derives the effective rate from
    # ``amount`` / ``subtotal_security`` when forming ``@@``, so the writer
    # doesn't strictly need this. Keeping it lets diagnostics flag drift
    # between Pictet's printed rate and the implied one.
    exchange_rate: Decimal | None = None

    # --- Internal-transfer cross-leg (only set on cross-currency book
    # transfers between the user's own current accounts) ------------------
    # On a Pictet ``Internal money transfer`` advice the document records
    # *two* CASH EFFECT blocks — one debit on the source account, one
    # credit on the destination account in a different currency. Modelling
    # this as a single ``Transaction`` with both currencies/amounts (rather
    # than two separate ``Transaction`` rows balanced against
    # ``Equity:Uncategorized``) lets the writer emit a single beancount
    # entry with the destination leg carrying ``@@ <abs_source> <src_ccy>``
    # to record the FX. ``currency``/``amount`` hold the source (debit)
    # leg signed-negative; these hold the destination (credit) leg
    # signed-positive.
    counter_currency: str | None = None
    counter_amount: Decimal | None = None

    # --- Bond accrued interest ------------------------------------------
    # Set on ``SELL_BONDS`` advices: the amount of accrued interest the
    # buyer pays to the seller alongside the bond's principal proceeds.
    # Pictet prints this on a dedicated ``Interest`` line inside the
    # ``CASH EFFECT`` block, on top of the percentage-priced principal.
    # Recognised by the writer as a separate ``Income:<prefix>:<isin>:Interest``
    # leg so the bond's running yield stays distinct from realised
    # capital gain/loss on the principal.
    accrued_interest: Decimal | None = None

    # --- Self-to-self payment cross-leg ---------------------------------
    # Set on outgoing ``PAYMENT`` advices where the ``Beneficiary``
    # matches the account holder name — the payment is a self-to-self
    # transfer to the user's own external account (e.g. Revolut).
    # ``gross_amount`` is the principal sent (signed positive — Pictet's
    # top-of-document gross line, distinct from the ``CASH EFFECT``
    # block's gross which is signed negative for the source-account
    # perspective). ``counter_account`` is the short account-name
    # segment for the destination bank, looked up from
    # :data:`banking_pipeline.config.settings.beneficiary_bank_map`
    # against the document's ``Bank`` field. The writer uses both to
    # emit a three-leg entry: destination credited with ``gross_amount``,
    # source debited with ``amount`` (net), Pictet's wire fee posted to
    # ``Expenses:<prefix>:Fees:<ccy>``. Both fields ``None`` on
    # genuine third-party outgoing payments — the writer falls back to
    # the elastic ``Expenses:<prefix>:Other`` shape for those.
    gross_amount: Decimal | None = None
    counter_account: str | None = None

    # --- Third-party counterparty routing ------------------------------
    # Set on third-party PAYMENT / INCOMING_PAYMENT / PAGO_ENTRANTE
    # advices when the printed counterparty name (``Beneficiary`` /
    # ``Instructing party`` / ``Ordenante``) matches a substring needle
    # in :data:`banking_pipeline.config.settings.counterparty_account_map`.
    # The mapped value is the account segment after the family root;
    # the writer prepends ``Income:`` (cash in) or ``Expenses:`` (cash
    # out) and emits the elastic counter-leg there instead of the
    # catch-all ``Income:<prefix>:<portfolio>:Other`` /
    # ``Expenses:<prefix>:<portfolio>:Other`` placeholder. ``None`` on
    # advices that don't resolve, falling back to the elastic shape.
    #
    # Distinct from ``counter_account`` (which carries an account-name
    # *segment* used in self-to-self routing as ``Assets:<segment>:<ccy>``);
    # ``counterparty_account`` carries a path *segment* used as
    # ``Income:<segment>`` / ``Expenses:<segment>``.
    counterparty_account: str | None = None

    # --- UK CGT cost basis ----------------------------------------------
    # GBP per 1 unit of ``currency`` at trade date. ``None`` means no rate
    # available; downstream builders fall back to current behaviour.
    gbp_rate: Decimal | None = None

    # --- Account identifiers --------------------------------------------
    account_number: str | None = None  # IBAN, broker account, etc.
    # Pictet's per-document reference (``N° de transacción``). Emitted by
    # the writer as a trailing ``  no: <number>`` comment on the entry.
    transaction_number: str | None = None
    # Beancount link (``^<id>`` after the narration). Used to thread
    # related entries together — most prominently the salida + entrada
    # legs of a switch, which share a single link so ``bean-query`` can
    # retrieve both with one filter. The extractor doesn't fill this from
    # the document alone (the legs reference each other through external
    # pairing, not through any in-document field); a higher pipeline layer
    # sets it after detecting a salida/entrada pair. When ``None`` the
    # writer falls back to ``transaction_number`` for switches and emits
    # no link at all for non-switch entries.
    link_id: str | None = None

    # --- Provenance -----------------------------------------------------
    source_path: Path
    source_page: int | None = None

    @property
    def is_fx(self) -> bool:
        """True when the security and cash-account currencies differ.

        The writer branches on this to choose between the simple two-posting
        template and the FX template that splits fees out and emits a
        beancount ``@@`` annotation on the cash leg. Returns ``False`` when
        ``security_currency`` is unset (non-security documents).
        """

        return (
            self.security_currency is not None
            and self.security_currency != self.currency
        )


class ExtractionResult(BaseModel):
    classification: Classification
    transactions: list[Transaction]
    warnings: list[str] = Field(default_factory=list)
    # The PDF this result came from. Stored at result-level (not just on each
    # ``Transaction``) so the source is still available when extraction yields
    # zero transactions — useful for the ``; source:`` header comment in the
    # rendered beancount output and for audit/diagnostic logs.
    source_path: Path
