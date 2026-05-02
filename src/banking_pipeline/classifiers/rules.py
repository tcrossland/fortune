"""Rule-based document classifier.

Each rule is a bag of compiled regexes with a weight. The document type with
the highest aggregated score wins; the raw score is normalised into a [0,1]
confidence using a saturating function tuned so that a full pattern match
(score=1.0) yields ~0.95 confidence — comfortably above the default 0.75
LLM-fallback threshold.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from banking_pipeline.models import BankId, Classification, DocumentType, RawDocument


@dataclass(frozen=True)
class Rule:
    doc_type: DocumentType
    template_id: str
    patterns: tuple[re.Pattern[str], ...]
    weight: float = 1.0
    # Bank this rule belongs to. ``None`` means bank-agnostic (i.e. always eligible).
    bank: BankId | None = None


# Bank-agnostic starter ruleset. Add one Rule per document type you want to
# recognise across any issuer.
GENERIC_RULES: tuple[Rule, ...] = (
    Rule(
        doc_type=DocumentType.TRADE_CONFIRMATION,
        template_id="generic.trade_confirmation.v1",
        patterns=(
            re.compile(r"\btrade\s+confirmation\b", re.I),
            re.compile(r"\bISIN[:\s]+[A-Z]{2}[A-Z0-9]{9}[0-9]\b"),
            re.compile(r"\b(buy|sell|bought|sold)\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.DIVIDEND_NOTICE,
        template_id="generic.dividend_notice.v1",
        patterns=(
            re.compile(r"\bdividend\b", re.I),
            re.compile(r"\bex[- ]date\b", re.I),
            re.compile(r"\bpayment\s+date\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.ACCOUNT_STATEMENT,
        template_id="generic.account_statement.v1",
        patterns=(
            re.compile(r"\baccount\s+statement\b", re.I),
            re.compile(r"\bopening\s+balance\b", re.I),
            re.compile(r"\bclosing\s+balance\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.WIRE_CONFIRMATION,
        template_id="generic.wire_confirmation.v1",
        patterns=(
            # ``\bIBAN\b`` would false-positive on any advice that just quotes
            # the account's IBAN (e.g. Pictet's redemption notice), so we keep
            # the wire-specific markers tight instead.
            re.compile(r"\bwire\s+(?:transfer|payment)\b", re.I),
            re.compile(r"\bSWIFT\b"),
            re.compile(r"\bbeneficiary\b", re.I),
        ),
    ),
)


# Pictet-specific rules. Their statements have distinctive fixed phrases
# (e.g. "Operation type Sale", "OUT of portfolio") that make for a tight
# match. Rules are grouped by the document's language because the patterns
# themselves are language-specific — keeping them in named sublists makes
# it obvious where to add a new locale and keeps PICTET_RULES composed as
# the flat concatenation that :class:`RuleClassifier` consumes.
PICTET_EN_RULES: tuple[Rule, ...] = (
    Rule(
        doc_type=DocumentType.REDEMPTION_NOTICE,
        template_id="pictet.redemption_notice.v1",
        bank=BankId.PICTET,
        patterns=(
            re.compile(r"\bredemption\b", re.I),
            re.compile(r"\boperation\s+type\s+sale\b", re.I),
            re.compile(r"\bexecuted\s+quantity\b", re.I),
            re.compile(r"\bexecution\s+price\b", re.I),
            re.compile(r"\bOUT\s+of\s+portfolio\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.SUBSCRIPTION_NOTICE,
        template_id="pictet.subscription_notice.v1",
        bank=BankId.PICTET,
        patterns=(
            re.compile(r"\bsubscription\b", re.I),
            re.compile(r"\boperation\s+type\s+purchase\b", re.I),
            re.compile(r"\bexecuted\s+quantity\b", re.I),
            re.compile(r"\bexecution\s+price\b", re.I),
            # Pictet writes "INportfolio" with no space — a reliable signal.
            re.compile(r"\bIN\s*portfolio\b"),
        ),
    ),
    # NOTE: ``SETTLE_FX_FORWARD`` must come BEFORE ``FX_FORWARD`` in this list.
    # Settlement advices are structurally identical to the opening forward
    # advice (same banner, same Asset sub-type, same Forex Forward narrative,
    # same Forward rate), so the plain ``FX_FORWARD`` rule would otherwise tie
    # at 5/5 on a settlement document. ``RuleClassifier`` keeps the first rule
    # that reaches ``best_score`` (strict ``>``), so listing SETTLE first lets
    # it claim ties on settlement docs while the plain forward rule still wins
    # on opening docs (where SETTLE's unique patterns miss).
    Rule(
        doc_type=DocumentType.SETTLE_FX_FORWARD,
        template_id="pictet.settle_fx_forward.v1",
        bank=BankId.PICTET,
        patterns=(
            # Title: "Settle FX forward".
            re.compile(r"\bSettle\s+FX\s+forward\b", re.I),
            # Settlement advices carry an ``Execution rate`` alongside the
            # ``Forward rate``; the opening advice does not.
            re.compile(r"\bExecution\s+rate\b", re.I),
            # Settlement-only cost line (P&L at settlement vs market rate).
            re.compile(r"\bForward\s+spread\b", re.I),
            # Asset-class banner shared with the opening forward.
            re.compile(r"\bAsset\s+sub-type\s+Foreign\s+exchange\b", re.I),
            # Security narrative — same as the opening advice; included so the
            # settle rule still scores 5/5 on the settlement document.
            re.compile(r"\bForex\s+Forward\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.FX_FORWARD,
        template_id="pictet.fx_forward.v1",
        bank=BankId.PICTET,
        patterns=(
            # Document title: "FX forward".
            re.compile(r"\bFX\s+forward\b", re.I),
            # Asset-class banner — distinguishes the forward advice from spot
            # FX trade confirmations, which use a different sub-type.
            re.compile(r"\bAsset\s+sub-type\s+Foreign\s+exchange\b", re.I),
            # "Buy notional USD ..." / "Sell notional GBP ..." — the two legs
            # are always present on an FX forward advice.
            re.compile(r"\b(?:Buy|Sell)\s+notional\b", re.I),
            # Forward-specific pricing field; spot tickets don't carry one.
            re.compile(r"\bForward\s+rate\b", re.I),
            # Security narrative used in the portfolio movement lines,
            # e.g. "Forex Forward USD/GBP, 05.02.26 (45985748)".
            re.compile(r"\bForex\s+Forward\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.SPOT,
        template_id="pictet.spot.v1",
        bank=BankId.PICTET,
        patterns=(
            # Banner — unique among FX advices (forwards use OTC DERIVATIVE).
            re.compile(r"\bFOREIGN\s+EXCHANGE\b"),
            # Title: "Spot".
            re.compile(r"^\s*Spot\s*$", re.M),
            # "Buy amount" / "Sell amount" — spot's leg labels; forwards use
            # "Buy notional" / "Sell notional" instead, so this is a clean
            # disambiguator against SETTLE_FX_FORWARD.
            re.compile(r"\b(?:Buy|Sell)\s+amount\b", re.I),
            # Market-place line characteristic of OTC FX spots at Pictet.
            re.compile(r"\bMarket\s+place\s+Over\s+the\s+counter\b", re.I),
            # Trade ID suffix is spot-specific at Pictet.
            re.compile(r"SPOTLUX\b"),
        ),
    ),
    Rule(
        doc_type=DocumentType.BUY_STRUCTURED_PRODUCTS,
        template_id="pictet.buy_structured_products.v1",
        bank=BankId.PICTET,
        patterns=(
            # Title.
            re.compile(r"\bBuy\s+structured\s+products\b", re.I),
            # Asset classification — stable across Pictet structured-product
            # variants (PEC certificates, notes, etc.).
            re.compile(r"\bAsset\s+type\s+Structured\s+products\b", re.I),
            # Distinguishes from a fund SUBSCRIPTION (which uses "Purchase").
            re.compile(r"\bOperation\s+type\s+Buy\b", re.I),
            # Structured-product-specific issuer line at Pictet.
            re.compile(r"\bIssuer\s+BANQUE\s+PICTET\b", re.I),
            # Maturity date is always quoted on structured-product advices.
            re.compile(r"\bMaturity\s+date\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.BUY_ETF,
        template_id="pictet.buy_etf.v1",
        bank=BankId.PICTET,
        patterns=(
            # Title — "Buy Exchange Traded Fund". Unique to ETF advices; the
            # structured-products variant uses "Buy structured products" and
            # the fund-subscription variant uses "Subscription".
            re.compile(r"\bBuy\s+Exchange\s+Traded\s+Fund\b", re.I),
            # Asset classification — the strongest discriminator vs the other
            # "Operation type Buy" advice (structured products) and vs fund
            # subscriptions (which carry a different asset type entirely).
            re.compile(r"\bAsset\s+type\s+Exchange\s+Traded\s+Fund\b", re.I),
            # Distinguishes from SUBSCRIPTION_NOTICE (which uses "Purchase");
            # shared with BUY_STRUCTURED_PRODUCTS, but the asset-type pattern
            # above keeps the two apart.
            re.compile(r"\bOperation\s+type\s+Buy\b", re.I),
            # Execution-block markers shared with all single-leg trade advices
            # — load-bearing only in combination with the title and asset-type
            # patterns, but kept so the rule denominator reaches 5 and the
            # saturating confidence reaches ~0.95 on a clean match.
            re.compile(r"\bExecuted\s+quantity\b", re.I),
            re.compile(r"\bExecution\s+price\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.BUY_SHARES,
        template_id="pictet.buy_shares.v1",
        bank=BankId.PICTET,
        patterns=(
            # Title — "Buy Shares". Unique to direct-equity advices; the
            # other ``Buy <type>`` variants use ``Buy structured products``
            # or ``Buy Exchange Traded Fund``.
            re.compile(r"\bBuy\s+Shares\b", re.I),
            # Asset classification — the load-bearing discriminator from
            # BUY_ETF and BUY_STRUCTURED_PRODUCTS (which carry their own
            # asset-type banners) and from SUBSCRIPTION_NOTICE (which is
            # explicitly a fund subscription, not a direct equity buy).
            re.compile(r"\bAsset\s+type\s+Equities\b", re.I),
            # Shared with BUY_ETF / BUY_STRUCTURED_PRODUCTS but distinct
            # from SUBSCRIPTION_NOTICE (which uses "Purchase").
            re.compile(r"\bOperation\s+type\s+Buy\b", re.I),
            re.compile(r"\bExecuted\s+quantity\b", re.I),
            re.compile(r"\bExecution\s+price\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.SELL_ETF,
        template_id="pictet.sell_etf.v1",
        bank=BankId.PICTET,
        patterns=(
            # Title — unique to ETF sales among the sell family.
            re.compile(r"\bSell\s+Exchange\s+Traded\s+Fund\b", re.I),
            # Asset classification — load-bearing discriminator from
            # SELL_STRUCTURED_PRODUCTS (which carries ``Asset type
            # Structured products``) and from REDEMPTION_NOTICE (whose
            # asset type is fund-shaped).
            re.compile(r"\bAsset\s+type\s+Exchange\s+Traded\s+Fund\b", re.I),
            # ``Sell`` (not ``Sale``) is the ETF operation vocabulary.
            re.compile(r"\bOperation\s+type\s+Sell\b", re.I),
            re.compile(r"\bExecuted\s+quantity\b", re.I),
            re.compile(r"\bExecution\s+price\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.SELL_STRUCTURED_PRODUCTS,
        template_id="pictet.sell_structured_products.v1",
        bank=BankId.PICTET,
        patterns=(
            # Title — unique to structured-product sales.
            re.compile(r"\bSell\s+structured\s+products\b", re.I),
            # Asset classification — load-bearing discriminator from
            # ``SELL_BONDS`` (which carries ``Asset type Bonds``) and
            # from ``REDEMPTION_NOTICE`` (which is fund-asset-typed and
            # uses ``Operation type Sale``).
            re.compile(r"\bAsset\s+type\s+Structured\s+products\b", re.I),
            # ``Sell`` (not ``Sale``) is the structured-product operation
            # vocabulary; ``REDEMPTION_NOTICE`` uses ``Sale``.
            re.compile(r"\bOperation\s+type\s+Sell\b", re.I),
            # Structured-product-specific issuer line at Pictet.
            re.compile(r"\bIssuer\s+BANQUE\s+PICTET\b", re.I),
            # Maturity date is always quoted on structured-product advices.
            re.compile(r"\bMaturity\s+date\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.BUY_BONDS,
        template_id="pictet.buy_bonds.v1",
        bank=BankId.PICTET,
        patterns=(
            # Banner — Pictet routes bond purchases through the
            # ``STOCK EXCHANGE`` desk (sells use ``SECURITY`` instead).
            re.compile(r"^STOCK\s+EXCHANGE\s*$", re.M),
            # Standalone title — anchored to a full line so the headline
            # ``Purchase EUR 90'000.00 ...`` doesn't accidentally match.
            # ``Purchase`` alone is unique to bond buys among the EN
            # trade family (subscriptions use ``Subscription``;
            # ETF/structured/shares use ``Buy <type>``).
            re.compile(r"^Purchase\s*$", re.M),
            # Bond-specific quantity field — also fires on SELL_BONDS,
            # but the title and operation-type patterns separate them.
            re.compile(r"\bExecuted\s+nominal\b", re.I),
            # Percentage-priced execution price — the structural
            # discriminator from any unit-priced trade. ``\d+\.?\d*\s*%``
            # is tight enough that the ``%`` makes it unambiguous.
            re.compile(r"^Execution\s+price\s+\d+\.?\d*\s*%\s*$", re.M),
            # ``Purchase`` operation-type vocabulary; bond sells say
            # ``Sell`` and ETF/structured/shares buys say ``Buy``.
            re.compile(r"\bOperation\s+type\s+Purchase\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.SELL_BONDS,
        template_id="pictet.sell_bonds.v1",
        bank=BankId.PICTET,
        patterns=(
            # Title — unique to bond-sale advices among the trade family.
            re.compile(r"\bSell\s+bonds\b", re.I),
            # Asset classification — load-bearing discriminator vs the
            # other ``Operation type Sell`` paths (REDEMPTION_NOTICE,
            # FINAL_REDEMPTION) which carry equity- or fund-typed assets.
            re.compile(r"\bAsset\s+type\s+Bonds\b", re.I),
            # Bond-specific quantity field — the regular trade advices
            # use ``Executed quantity`` (unit count) instead.
            re.compile(r"\bExecuted\s+nominal\b", re.I),
            # Distinguishes from a (hypothetical future) ``BUY_BONDS``
            # variant which would carry ``Operation type Buy``.
            re.compile(r"\bOperation\s+type\s+Sell\b", re.I),
            # Maturity date is always quoted on bond advices; shared with
            # structured-product buys but the title + asset-type patterns
            # above already separate the two.
            re.compile(r"\bMaturity\s+date\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.DIVIDEND_NOTICE,
        template_id="pictet.dividend_notice.v1",
        bank=BankId.PICTET,
        patterns=(
            # Banner "SECURITY EVENT" / sub-banner "Distribution" /
            # sub-sub-banner "Ordinary dividend".
            re.compile(r"\bSECURITY\s+EVENT\b"),
            re.compile(r"^\s*Distribution\s*$", re.M),
            re.compile(r"\b(?:Ordinary|Extraordinary|Special)\s+dividend\b", re.I),
            # Income-per-unit is unique to distributions (not redemptions).
            re.compile(r"\bIncome\s+per\s+unit\b", re.I),
            # Quantity-held is Pictet's label for the underlying holding that
            # generated the distribution.
            re.compile(r"\bQuantity\s+held\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.FINAL_REDEMPTION,
        template_id="pictet.final_redemption.v1",
        bank=BankId.PICTET,
        patterns=(
            # Title: "Final redemption" — distinguishes from a plain
            # fund-sale ``REDEMPTION_NOTICE``.
            re.compile(r"\bFinal\s+redemption\b", re.I),
            # Redemption-event-specific pricing field (vs "Execution price").
            re.compile(r"\bRedemption\s+price\b", re.I),
            # Shared with DIVIDEND_NOTICE but still helpful for this rule's
            # denominator.
            re.compile(r"\bSECURITY\s+EVENT\b"),
            # Reference holding block — characteristic of security events.
            re.compile(r"\bREFERENCE\s+HOLDING\s*in\s+portfolio\b"),
            # The security actually leaves the portfolio on final redemption.
            re.compile(r"\bOUT\s+of\s+portfolio\b"),
        ),
    ),
    Rule(
        doc_type=DocumentType.DEBIT_OF_FEES,
        template_id="pictet.debit_of_fees.v1",
        bank=BankId.PICTET,
        patterns=(
            # Title.
            re.compile(r"\bDebit\s+of\s+fees\b", re.I),
            # Banner — standalone "FEES" line.
            re.compile(r"^\s*FEES\s*$", re.M),
            re.compile(r"\bAdministration\s+flat\s+fee\b", re.I),
            re.compile(r"\bAccount\s+maintenance\s+fees?\b", re.I),
            # "Flat fees 1st quarter 2026" comment pattern — the "Flat fees"
            # phrase is specific to this advice type at Pictet.
            re.compile(r"\bFlat\s+fees\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.INTEREST_PAYMENT,
        template_id="pictet.interest_payment.v1",
        bank=BankId.PICTET,
        patterns=(
            re.compile(r"\bInterest\s+payment\b", re.I),
            # The two balance-split interest lines are unique to the payment
            # advice; the scale document doesn't break them out this way.
            re.compile(r"\bInterest\s+\(on\s+credit\s+balance\)", re.I),
            re.compile(r"\bInterest\s+\(on\s+debit\s+balance\)", re.I),
            # Shared with INTEREST_SCALE — kept so the rule reaches 5 patterns.
            re.compile(r"\bCalculation\s+basis\b", re.I),
            re.compile(r"\bEffective\s+days\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.INTEREST_SCALE,
        template_id="pictet.interest_scale.v1",
        bank=BankId.PICTET,
        patterns=(
            re.compile(r"\bInterest\s+scale\b", re.I),
            # Column headers from the per-bucket interest table.
            re.compile(r"\bPERIOD\s+DAYS\s+BALANCE\b"),
            re.compile(r"\bRATE\s*\(%\)"),
            # Shared with INTEREST_PAYMENT.
            re.compile(r"\bCalculation\s+basis\b", re.I),
            re.compile(r"\bEffective\s+days\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.LIMIT_EXTENSION,
        template_id="pictet.limit_extension.v1",
        bank=BankId.PICTET,
        patterns=(
            # Banner — standalone "LIMIT" line.
            re.compile(r"^\s*LIMIT\s*$", re.M),
            # Lombard credit paperwork boilerplate.
            re.compile(r"\bCredit\s+Agreement\b"),
            # "C/a limit" = current-account limit, Pictet's shorthand.
            re.compile(r"\bC/a\s+limit\b"),
            # Extension advices always quote the previous opening date
            # alongside a modification date.
            re.compile(r"\bModification\s+date\b", re.I),
            re.compile(r"\bOpening\s+date\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.INCOMING_PAYMENT,
        template_id="pictet.incoming_payment.v1",
        bank=BankId.PICTET,
        patterns=(
            re.compile(r"\bPAYMENT\s+TRANSACTIONS\b"),
            re.compile(r"\bIncoming\s+payment\b", re.I),
            # On incoming wires the other party is the "instructing party",
            # never a "beneficiary" (that's the outgoing-PAYMENT marker).
            re.compile(r"\bInstructing\s+party\b", re.I),
            re.compile(r"\bPayment\s+reference\b", re.I),
            re.compile(r"\bBank\s+clearing\s+no\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.PAYMENT,
        template_id="pictet.payment.v1",
        bank=BankId.PICTET,
        patterns=(
            re.compile(r"\bPAYMENT\s+TRANSACTIONS\b"),
            # Outgoing-only: beneficiary field + IBAN of the destination.
            re.compile(r"\bBeneficiary\b", re.I),
            re.compile(r"\bPayment\s+fees\b", re.I),
            # Outgoing advices carry a free-text ``Communication`` line;
            # incoming advices use ``Payment reference`` instead.
            re.compile(r"\bCommunication\b"),
            # Destination IBAN quoted as a proper IBAN (not Pictet's
            # ``IBAN?LU...`` own-account shorthand).
            re.compile(r"\bIBAN\s+[A-Z]{2}\d{2}\s"),
        ),
    ),
    Rule(
        doc_type=DocumentType.INTERNAL_TRANSFER,
        template_id="pictet.internal_transfer.v1",
        bank=BankId.PICTET,
        patterns=(
            # Shared banner with PAYMENT / INCOMING_PAYMENT — load-bearing
            # only in combination with the title below.
            re.compile(r"\bPAYMENT\s+TRANSACTIONS\b"),
            # Title — unique; neither PAYMENT nor INCOMING_PAYMENT carries it.
            re.compile(r"\bInternal\s+money\s+transfer\b", re.I),
            # Both legs post as ``CASH EFFECT`` entries back into client-owned
            # portfolios; the Pictet template glues the words together with no
            # space ("CASH EFFECTin portfolio"), so we don't require one.
            re.compile(r"\bCASH\s+EFFECT"),
            # FX leg marker — internal transfers always cross currencies at
            # Pictet (same-currency moves are booked as a simple adjustment,
            # not as a PAYMENT TRANSACTIONS advice).
            re.compile(r"\bExchange\s+rate\b", re.I),
            # Sub-total line appears between the two portfolio legs; absent
            # from single-leg PAYMENT / INCOMING_PAYMENT advices.
            re.compile(r"\bSub-total\b", re.I),
        ),
    ),
    # NOTE: ``ANNUAL_STATEMENT`` must come BEFORE ``MONTHLY_STATEMENT`` and
    # ``QUARTERLY_STATEMENT``. All three share the ``Financial Statement``
    # banner, and quarterly's regulatory-block patterns ALSO fire on the
    # annual fixture (annual reuses the same ``Client classification`` /
    # ``Client profile`` / ``Risk appetite`` / ``Time horizon`` page). That
    # means on an annual fixture the quarterly rule ties at 5/5, and we rely
    # on the strict ``>`` tie-break to keep annual listed first. Monthly is
    # safe on its own merits (annual hits 1/5 on the monthly fixture and
    # monthly hits 1/5 on the annual fixture via the shared banner only).
    Rule(
        doc_type=DocumentType.ANNUAL_STATEMENT,
        template_id="pictet.annual_statement.v1",
        bank=BankId.PICTET,
        patterns=(
            # Shared banner with the monthly and quarterly advices.
            re.compile(r"\bFinancial\s+Statement\b", re.I),
            # Mandate product name — only the annual statement shows the
            # discretionary-mandate header on the ESG disclosure page.
            re.compile(r"\bResponsible\s+Investing\b", re.I),
            # EU-mandated sustainability disclosures — annual-only.
            re.compile(r"\bEU\s+Taxonomy\b"),
            re.compile(r"\bSFDR\b"),
            # Section shared with the quarterly statement; monthly shows only
            # one per-currency current-account statement while annual and
            # quarterly both aggregate them under this heading.
            re.compile(r"\bSummary\s+of\s+current\s+accounts\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.QUARTERLY_STATEMENT,
        template_id="pictet.quarterly_statement.v1",
        bank=BankId.PICTET,
        patterns=(
            # Shared banner.
            re.compile(r"\bFinancial\s+Statement\b", re.I),
            # MiFID regulatory-profile page — present on both quarterly and
            # annual, absent from the monthly statement. All four fields
            # below are authored as one block, so a quarterly fixture reliably
            # scores all of them.
            re.compile(r"\bClient\s+classification\b", re.I),
            re.compile(r"\bClient\s+profile\b", re.I),
            re.compile(r"\bRisk\s+appetite\b", re.I),
            re.compile(r"\bTime\s+horizon\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.ORDER_INFORMATION_REPORT,
        template_id="pictet.order_information_report.v1",
        bank=BankId.PICTET,
        patterns=(
            # Title — also repeats as the running page header, so it's the
            # most reliable single marker.
            re.compile(r"\bOrder\s+information\s+report\b", re.I),
            # Section heading for the proposed BUY/SELL legs.
            re.compile(r"\bYour\s+trade\s+instruction\b", re.I),
            # Cash-impact block — unique to the pre-trade disclosure; the
            # post-trade subscription/redemption advices book the leg into
            # ``CASH EFFECT in portfolio`` instead.
            re.compile(r"\bIMPACT\s+ON\s+CASH\b"),
            # Forward-looking cost simulation — absent from the ``DEBIT_OF_FEES``
            # / ``FACTURA`` fee advices, which report realised charges rather
            # than simulate future ones.
            re.compile(r"\bCosts?\s+simulation\b", re.I),
            # Field label in the simulation table.
            re.compile(r"\bInvestment\s+Period\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.MONTHLY_STATEMENT,
        template_id="pictet.monthly_statement.v1",
        bank=BankId.PICTET,
        patterns=(
            re.compile(r"\bFinancial\s+Statement\b", re.I),
            # Executive summary page — absent from annual (annual's first
            # content page is the ESG disclosure instead).
            re.compile(r"\bExecutive\s+summary\b", re.I),
            # Benchmark-index performance page — monthly-only. The OCR
            # occasionally glues the words ("benchmarkindices"), so the
            # space between "benchmark" and "indices" is optional.
            re.compile(r"\bPerformance\s+of\s+benchmark\s*indices\b", re.I),
            # Quarterly/annual performance block — monthly carries this as a
            # trailing historical view; the annual statement doesn't.
            re.compile(r"\bQuarterly\s+and\s+annual\s+performance\b", re.I),
            # Full portfolio valuation section — absent from the annual
            # template (which leans on the mandate's analytical pages).
            re.compile(r"\bPortfolio\s+valuation\b", re.I),
        ),
    ),
)


PICTET_ES_RULES: tuple[Rule, ...] = (
    Rule(
        doc_type=DocumentType.SWITCH_SALIDA,
        template_id="pictet.switch_salida.v1",
        bank=BankId.PICTET,
        patterns=(
            # Document title: `Cambio ("switch") de fondos (salida)`. Kept
            # permissive across the parenthesised words so minor formatting
            # drift doesn't break the match.
            re.compile(r"\bcambio\b.*switch.*salida", re.I),
            re.compile(r"\bSwitch\s+entre\b", re.I),  # "Switch entre X y Y"
            re.compile(r"\bSALIDA\s*de\s+la\s+cartera\b", re.I),  # structural
            # ``Tipo de operación Venta`` (2023+) and ``Tipo de ejecución Venta``
            # (2021-era) are the same field renamed across Pictet ES generations.
            # Matching either keeps older fixtures at full 5/5 confidence.
            re.compile(
                r"\btipo\s+de\s+(?:operaci[oó]n|ejecuci[oó]n)\s+venta\b", re.I
            ),
            re.compile(r"\bcantidad\s+ejecutada\b", re.I),
        ),
    ),
    # Paired leg — no fixture yet; keeping a sketch so the registry stays
    # symmetric and the template_id namespace is reserved.
    Rule(
        doc_type=DocumentType.SWITCH_ENTRADA,
        template_id="pictet.switch_entrada.v1",
        bank=BankId.PICTET,
        patterns=(
            re.compile(r"\bcambio\b.*switch.*entrada", re.I),
            re.compile(r"\bSwitch\s+entre\b", re.I),
            re.compile(r"\bENTRADA\s*en\s+la\s+cartera\b", re.I),
            # Same dual-form as SWITCH_SALIDA; see comment there.
            re.compile(
                r"\btipo\s+de\s+(?:operaci[oó]n|ejecuci[oó]n)\s+compra\b", re.I
            ),
            re.compile(r"\bcantidad\s+ejecutada\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.SUSCRIPCION,
        template_id="pictet.suscripcion.v1",
        bank=BankId.PICTET,
        patterns=(
            # Banner — also fires on COMPRA / REEMBOLSO / SWITCH_* so it is
            # load-bearing only in combination with the others.
            re.compile(r"\bBOLSA\s+DE\s+VALORES\b", re.I),
            # Title — unique to this advice type.
            re.compile(r"\bSuscripci[oó]n\b", re.I),
            # ``Tipo de operación Compra`` (2023+) and ``Tipo de ejecución Compra``
            # (2021-era) are the same field renamed across generations.
            re.compile(
                r"\btipo\s+de\s+(?:operaci[oó]n|ejecuci[oó]n)\s+compra\b", re.I
            ),
            re.compile(r"\bENTRADA\s*en\s+la\s+cartera\b", re.I),
            re.compile(r"\bcantidad\s+ejecutada\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.COMPRA,
        template_id="pictet.compra.v1",
        bank=BankId.PICTET,
        patterns=(
            re.compile(r"\bBOLSA\s+DE\s+VALORES\b", re.I),
            # Standalone ``Compra`` title line — the load-bearing
            # discriminator from SUSCRIPCION (whose title is
            # ``Suscripción``). Anchored to a full line via ``^...$ +
            # re.M`` so the headline ``Compra 567 ...`` doesn't match,
            # and case-insensitive so 2022-era fixtures that print the
            # title as ``COMPRA`` still hit. Earlier this rule relied on
            # ``Corretaje`` / ``Tasa bursátil`` for separation, but those
            # lines are absent on zero-fee stock buys (Costes EUR 0.00),
            # which let SUSCRIPCION beat COMPRA on the 2022 fixture.
            re.compile(r"^Compra\s*$", re.M | re.I),
            re.compile(
                r"\btipo\s+de\s+(?:operaci[oó]n|ejecuci[oó]n)\s+compra\b", re.I
            ),
            re.compile(r"\bENTRADA\s*en\s+la\s+cartera\b", re.I),
            re.compile(r"\bcantidad\s+ejecutada\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.REEMBOLSO,
        template_id="pictet.reembolso.v1",
        bank=BankId.PICTET,
        patterns=(
            re.compile(r"\bBOLSA\s+DE\s+VALORES\b", re.I),
            # Title — distinguishes from a switch-salida, which also uses
            # ``Venta`` + ``SALIDA de la cartera`` but opens with
            # ``Cambio ("switch") de fondos (salida)``.
            re.compile(r"\bReembolso\b", re.I),
            re.compile(
                r"\btipo\s+de\s+(?:operaci[oó]n|ejecuci[oó]n)\s+venta\b", re.I
            ),
            re.compile(r"\bSALIDA\s*de\s+la\s+cartera\b", re.I),
            re.compile(r"\bcantidad\s+ejecutada\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.VENTA,
        template_id="pictet.venta.v1",
        bank=BankId.PICTET,
        patterns=(
            re.compile(r"\bBOLSA\s+DE\s+VALORES\b", re.I),
            # Standalone ``Venta`` title — sell counterpart to ``COMPRA``'s
            # ``^Compra\s*$`` discriminator. The load-bearing distinction
            # from REEMBOLSO (fund redemption), whose title is
            # ``Reembolso``. Anchored to a full line so the headline
            # ``Venta -119 ...`` doesn't match.
            re.compile(r"^Venta\s*$", re.M | re.I),
            re.compile(
                r"\btipo\s+de\s+(?:operaci[oó]n|ejecuci[oó]n)\s+venta\b", re.I
            ),
            re.compile(r"\bSALIDA\s*de\s+la\s+cartera\b", re.I),
            re.compile(r"\bcantidad\s+ejecutada\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.DISTRIBUCION,
        template_id="pictet.distribucion.v1",
        bank=BankId.PICTET,
        patterns=(
            # Banner — shared with REEMBOLSO_FINAL and (rare future ES
            # security-event variants); load-bearing only in combination
            # with the title patterns below.
            re.compile(r"^\s*HECHOS\s+RELEVANTES\s*$", re.M | re.I),
            # Subtitle — the load-bearing discriminator from REEMBOLSO_FINAL
            # (whose subtitle is ``Reembolso final``). Anchored to a full
            # line so unrelated mentions of the word don't false-match.
            re.compile(r"^Distribuci[oó]n\s*$", re.M | re.I),
            # ES counterpart of ``Ordinary dividend`` / ``Extraordinary``
            # / ``Special`` from the EN DIVIDEND_NOTICE rule.
            re.compile(r"\bDividendo\s+(?:ordinario|extraordinario|especial)\b", re.I),
            # Income-per-unit line — unique to distributions among ES
            # security-event advices (reembolso_final uses ``Precio de
            # rembolso`` instead).
            re.compile(r"\bRenta\s+unitaria\b", re.I),
            # Held-quantity field — ``Cantidad detenida`` is Pictet's ES
            # label for the underlying position that generated the
            # distribution. Shared with reembolso_final's ``CANTIDAD
            # DETENIDA`` block but the four patterns above already
            # separate this rule from that one.
            re.compile(r"\bCantidad\s+detenida\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.REEMBOLSO_FINAL,
        template_id="pictet.reembolso_final.v1",
        bank=BankId.PICTET,
        patterns=(
            # Banner — distinguishes from the trade-advice family which uses
            # ``BOLSA DE VALORES``. ``HECHOS RELEVANTES`` is Pictet's
            # security-event banner (matches the EN ``SECURITY EVENT``).
            re.compile(r"^\s*HECHOS\s+RELEVANTES\s*$", re.M | re.I),
            # Subtitle — the load-bearing discriminator from REEMBOLSO,
            # which also has ``Reembolso`` standalone but no ``final``
            # qualifier. Anchored to a full line so the section banner
            # ``REEMBOLSO`` (uppercase) doesn't accidentally match.
            re.compile(r"^Reembolso\s+final\s*$", re.M | re.I),
            # Pictet's apparent typo: ``Precio de rembolso`` (missing the
            # second ``e`` after the first one). Tolerant pattern handles
            # both spellings so corrected documents continue to match.
            re.compile(r"\bPrecio\s+de\s+re?embolso\b", re.I),
            # Held-quantity section unique to this advice type.
            re.compile(r"\bCANTIDAD\s+DETENIDA\b"),
            # Units leaving the portfolio. Shared with REEMBOLSO and
            # SWITCH_SALIDA, but the four patterns above already separate
            # this rule from both.
            re.compile(r"\bSALIDA\s*de\s+la\s+cartera\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.DEBITO_DE_GASTOS,
        template_id="pictet.debito_de_gastos.v1",
        bank=BankId.PICTET,
        patterns=(
            # Banner — standalone "GASTOS" line (distinct from the lowercase
            # mentions of ``gastos`` that appear in sub-headings).
            re.compile(r"^\s*GASTOS\s*$", re.M),
            # Title.
            re.compile(r"\bD[eé]bito\s+de\s+gastos\b", re.I),
            re.compile(r"\bHonorarios\s+de\s+administraci[oó]n\b", re.I),
            re.compile(r"\bComisiones\s+de\s+mantenimiento\b", re.I),
            # Pictet ES structural marker — shared with all ES advices but
            # pulls the rule's denominator up to 5.
            re.compile(r"\bEFECTO\s+CASH\s*en\s+la\s+cartera\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.FACTURA,
        template_id="pictet.factura.v1",
        bank=BankId.PICTET,
        patterns=(
            # Banner — the Madrid succursale issues fees as a tax-compliant
            # "FACTURA" (invoice), distinct from Luxembourg's "GASTOS"
            # debit-of-fees advice. NIF (Spanish tax ID) is the giveaway.
            re.compile(r"^\s*FACTURA\s*$", re.M),
            re.compile(r"\bN°\s*de\s+factura\b", re.I),
            re.compile(r"\bHonorarios\s+de\s+gesti[oó]n\b", re.I),
            re.compile(r"\bBase\s+de\s+c[aá]lculo\b", re.I),
            re.compile(r"\bValor\s+medio\s+de\s+la\s+cartera\b", re.I),
        ),
    ),
    # "Pago interna" — the Spanish-locale incoming-payment advice where the
    # ``Ordenante`` is the client themselves (self-to-self transfer from a
    # client-owned external account like Revolut). Pictet prints the title
    # in ALL CAPS (``PAGO ENTRANTE``) on this variant; the third-party
    # ``PAGO_ENTRANTE`` variant uses mixed case (``Pago entrante``). The
    # title's case-sensitivity is the load-bearing discriminator — we
    # deliberately drop ``re.I`` from that pattern to keep the two rules
    # from tying on the shared structural markers.
    Rule(
        doc_type=DocumentType.PAGO_INTERNA,
        template_id="pictet.pago_interna.v1",
        bank=BankId.PICTET,
        patterns=(
            # Banner — shared with any Pictet ES payment advice (outgoing or
            # incoming); load-bearing only in combination with the title.
            re.compile(r"\bTR[ÁA]FICO\s+DE\s+PAGOS\b", re.I),
            # Title — case-sensitive ``PAGO ENTRANTE`` (all caps, on its
            # own line). See the rule's preamble for why ``re.I`` is
            # intentionally absent.
            re.compile(r"^PAGO\s+ENTRANTE\s*$", re.M),
            # Ordering-party field — the giveaway that this is an *incoming*
            # advice (an outgoing "pago saliente" would carry ``Beneficiario``
            # here instead).
            re.compile(r"\bOrdenante\b", re.I),
            # Free-text payment-reference line — present on the self-to-self
            # variant (Revolut prints an ``IP<digits>`` reference); often
            # absent on the third-party variant (PAGO_ENTRANTE), making
            # this an additional structural discriminator.
            re.compile(r"\bReferencia\s+de\s+pago\b", re.I),
            # Shared Pictet ES structural marker — the credit leg posts into
            # a client-owned ``EFECTO CASH en la cartera`` current account.
            re.compile(r"\bEFECTO\s+CASH\s*en\s+la\s+cartera\b", re.I),
        ),
    ),
    # "Pago entrante" — third-party incoming payment (employer earnout,
    # vendor invoice settlement, etc.) where the ``Ordenante`` is a real
    # external counterparty. Pictet prints the title in mixed case
    # (``Pago entrante``) on this variant — distinct from the all-caps
    # ``PAGO ENTRANTE`` of the self-to-self ``PAGO_INTERNA`` variant. The
    # case-sensitive title match keeps the two rules from tying on shared
    # patterns; the absence of ``Referencia de pago`` (replaced here by
    # ``Banco`` / ``Pais`` lines for the foreign correspondent) is a
    # secondary structural signal.
    Rule(
        doc_type=DocumentType.PAGO_ENTRANTE,
        template_id="pictet.pago_entrante.v1",
        bank=BankId.PICTET,
        patterns=(
            re.compile(r"\bTR[ÁA]FICO\s+DE\s+PAGOS\b", re.I),
            # Title — case-sensitive mixed-case ``Pago entrante``.
            re.compile(r"^Pago\s+entrante\s*$", re.M),
            re.compile(r"\bOrdenante\b", re.I),
            # ``Banco`` — the correspondent bank that initiated the
            # third-party payment. Present on this variant; usually
            # absent on the self-to-self variant (which doesn't need a
            # correspondent because the source is the user's own
            # external account).
            re.compile(r"\bBanco\b", re.I),
            re.compile(r"\bEFECTO\s+CASH\s*en\s+la\s+cartera\b", re.I),
        ),
    ),
    # ES-locale outgoing third-party payment — Pictet prints the title
    # as a bare ``Pago`` (mixed case, on its own line) under the
    # ``TRÁFICO DE PAGOS`` banner. The two-character title is short but
    # the load-bearing distinction from the incoming variants is the
    # ``Beneficiario`` field (vs the incoming variants' ``Ordenante``);
    # the title's ``^Pago\s*$`` anchor and the case-sensitivity rule
    # the case-shifted ``PAGO ENTRANTE`` / mixed-case ``Pago entrante``
    # variants out.
    Rule(
        doc_type=DocumentType.PAGO,
        template_id="pictet.pago.v1",
        bank=BankId.PICTET,
        patterns=(
            re.compile(r"\bTR[ÁA]FICO\s+DE\s+PAGOS\b", re.I),
            # Title — case-sensitive bare ``Pago`` on its own line.
            # ``re.I`` deliberately omitted so this doesn't match the
            # all-caps ``PAGO ENTRANTE`` self-to-self variant.
            re.compile(r"^Pago\s*$", re.M),
            # The load-bearing distinction from incoming wires
            # (which use ``Ordenante``).
            re.compile(r"\bBeneficiario\b", re.I),
            # ES-specific outgoing-fee label — distinguishes from the
            # incoming variants which don't carry ES-locale fees on
            # the receiving side.
            re.compile(r"\bGastos\s+de\s+pago\b", re.I),
            # Free-text wire memo, present on outgoing advices and
            # absent (or named differently) on incoming.
            re.compile(r"\bComunicaci[oó]n\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.ESTADO_MENSUAL,
        template_id="pictet.estado_mensual.v1",
        bank=BankId.PICTET,
        patterns=(
            # Shared banner with the quarterly statement.
            re.compile(r"\bESTADO\s+FINANCIERO\b"),
            # Overview page — monthly-only. The quarterly report opens with
            # ``Recapitulación de las cuentas corrientes`` instead.
            re.compile(r"\bVista\s+preliminar\b", re.I),
            # Benchmark-index section — monthly-only.
            re.compile(r"\bEvoluci[oó]n\s+de\s+los\s+[ií]ndices\s+de\s+referencia\b", re.I),
            # Historical performance block — monthly-only.
            re.compile(r"\bResultados\s+trimestrales\s+y\s+anuales\b", re.I),
            # Full-portfolio valuation section heading — monthly carries the
            # detailed holdings list; the quarterly statement doesn't.
            re.compile(r"\bValoraci[oó]n\s+de\s+la\s+cartera\b", re.I),
        ),
    ),
    # NOTE: ``ESTADO_ANUAL`` must come BEFORE ``ESTADO_TRIMESTRAL``. The two
    # Spanish-locale statements are structurally identical — same TOC, same
    # ``Recapitulación`` + regulatory block, same disclaimer — so every
    # pattern on the trimestral rule ALSO fires on the anual fixture. The
    # only asymmetry is the banner date range: anual spans a full year
    # (``DEL <d> ENERO <y> AL <d> DICIEMBRE <y>``) where trimestral spans
    # three months. That year-range pattern is what lets the anual rule
    # reach 5/5 on the anual fixture while failing on trimestral; listing
    # anual first then lets the strict ``>`` tie-break pick it on the
    # shared 5/5 scores that result.
    Rule(
        doc_type=DocumentType.ESTADO_ANUAL,
        template_id="pictet.estado_anual.v1",
        bank=BankId.PICTET,
        patterns=(
            re.compile(r"\bESTADO\s+FINANCIERO\b"),
            # Year-spanning date range — the one marker absent from the
            # trimestral fixture. Pattern kept permissive on the day numbers
            # and the year so it survives fixture anonymisation (digits
            # masked to 9) and any real-world variant that opens the year on
            # the 1st vs the 2nd etc.
            re.compile(r"\bDEL\s+\d+\s+ENERO\s+\d+\s+AL\s+\d+\s+DICIEMBRE\b", re.I),
            # Shared with trimestral — kept so the rule denominator is 5 and
            # the saturating confidence reaches ~0.95 on a full match.
            re.compile(r"\bRecapitulaci[oó]n\s+de\s+las\s+cuentas\s+corrientes\b", re.I),
            re.compile(r"\bClasificaci[oó]n\s+cliente\b", re.I),
            re.compile(r"\bPerfil\s+cliente\b", re.I),
        ),
    ),
    Rule(
        doc_type=DocumentType.ESTADO_TRIMESTRAL,
        template_id="pictet.estado_trimestral.v1",
        bank=BankId.PICTET,
        patterns=(
            re.compile(r"\bESTADO\s+FINANCIERO\b"),
            # Current-accounts recap — distinctive to the quarterly report.
            re.compile(r"\bRecapitulaci[oó]n\s+de\s+las\s+cuentas\s+corrientes\b", re.I),
            # Regulatory-block banner — the monthly statement doesn't carry
            # this page (client-profile fields live on the annual/quarterly
            # cadence only).
            re.compile(r"\bInformaciones\s+reglamentarias\b", re.I),
            # MiFID client-classification line — quarterly-specific.
            re.compile(r"\bClasificaci[oó]n\s+cliente\b", re.I),
            # Client-profile header — pairs with "Apetito de riesgo",
            # "Horizonte de inversión" below it.
            re.compile(r"\bPerfil\s+cliente\b", re.I),
        ),
    ),
)


# Flat concatenation consumed by ``RuleClassifier``. Per-language patterns
# don't collide with each other in practice — English regex won't fire on
# Spanish text and vice-versa — so a single combined list is fine today. If
# that ever stops being true, :class:`LayeredClassifier` already knows the
# document language and is the natural place to gate rule selection.
PICTET_RULES: tuple[Rule, ...] = PICTET_EN_RULES + PICTET_ES_RULES


# Per-bank ruleset registry. The two-stage classifier looks up the bank first,
# then runs ``RULESETS_BY_BANK[bank] + GENERIC_RULES`` against the document.
RULESETS_BY_BANK: dict[BankId, tuple[Rule, ...]] = {
    BankId.PICTET: PICTET_RULES,
}


# Everything the single-stage classifier evaluates by default. Compose per-bank
# rulesets into this tuple so ``RuleClassifier()`` keeps working out-of-the-box.
DEFAULT_RULES: tuple[Rule, ...] = GENERIC_RULES + PICTET_RULES


@dataclass
class RuleClassifier:
    rules: tuple[Rule, ...] = field(default_factory=lambda: DEFAULT_RULES)

    def classify(self, doc: RawDocument) -> Classification:
        best_type = DocumentType.UNKNOWN
        best_template: str | None = None
        best_score = 0.0

        for rule in self.rules:
            hits = sum(1 for p in rule.patterns if p.search(doc.text))
            if hits == 0:
                continue
            score = rule.weight * hits / len(rule.patterns)
            if score > best_score:
                best_score = score
                best_type = rule.doc_type
                best_template = rule.template_id

        # Saturating confidence. With k=3 a full 5/5 match (score=1.0) → ~0.95,
        # 3/5 → ~0.83, 2/5 → ~0.70, 1/5 → ~0.45. The plain ``1 - exp(-x)`` curve
        # we used earlier caps at 0.63 for score=1.0 and never cleared the 0.75
        # LLM-fallback threshold, which defeated the purpose of the rules tier.
        confidence = 1.0 - math.exp(-3.0 * best_score)

        return Classification(
            document_type=best_type,
            confidence=confidence,
            source="rules",
            template_id=best_template,
        )
