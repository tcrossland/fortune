"""Map Revolut (Product, Currency) to beancount account names.

Convention chosen by the user: ``Assets:Revolut:Personal[:<Product>]:<CCY>``,
where ``<Product>`` is omitted for the main current accounts, ``Pro`` for the
Revolut Pro pocket, and ``FlexibleCash`` for the Flexible Cash Funds. Anything
else falls back to the literal ``Product`` segment so unknown product types
produce parseable (if non-canonical) accounts the user can rename.
"""

from __future__ import annotations

from typing import Final

# Canonical roots used elsewhere in the ledger.
EXPENSES_FIXME: Final[str] = "Expenses:FIXME"
INCOME_FIXME: Final[str] = "Income:FIXME"
EXPENSES_FEES: Final[str] = "Expenses:Fees:Revolut"
INCOME_INTEREST: Final[str] = "Income:Revolut:Interest"
INCOME_CASHBACK: Final[str] = "Income:Revolut:Cashback"
EQUITY_OPENING: Final[str] = "Equity:Opening-Balances"

# Product values we recognise from the CSV's ``Product`` column. Mapping is to
# the segment that goes between ``Assets:Revolut:Personal:`` and the currency.
# Empty string means "no extra segment" (i.e. main current account).
_PRODUCT_SEGMENT: Final[dict[str, str]] = {
    "Current": "",
    "Pro": "Pro",
    "Savings": "FlexibleCash",
    # Revolut sometimes labels the savings/flexible-cash product differently
    # depending on jurisdiction and export vintage. Treat these aliases as
    # the same destination so the user doesn't get split account trees.
    "Flexible Account": "FlexibleCash",
    "Flexible Cash Funds": "FlexibleCash",
}


def asset_account(product: str, currency: str) -> str:
    """Return the beancount asset account for a (product, currency) pair.

    Unknown ``product`` values fall through to a sanitised form of the
    literal product name so the account is still parseable; the user can
    rename via ``rename`` directives once they decide on a canonical home.
    """

    segment = _PRODUCT_SEGMENT.get(product)
    if segment is None:
        # Sanitise: drop non-alphanumerics so the segment is a valid
        # beancount account component, then normalise to a single
        # capitalised word ("Crypto Stuff" → "Cryptostuff") so multi-word
        # products produce one stable segment instead of leaking internal
        # whitespace or mixed case into the account tree.
        collapsed = "".join(ch for ch in product if ch.isalnum())
        segment = (collapsed[:1].upper() + collapsed[1:].lower()) if collapsed else "Other"
    parts = ["Assets", "Revolut", "Personal"]
    if segment:
        parts.append(segment)
    parts.append(currency)
    return ":".join(parts)
