"""Bank-specific writer configuration.

A :class:`BankWriterProfile` bundles every piece of writer behaviour that
varies by issuing bank — today just the ``account_prefix`` segment used
in ``Assets:<prefix>:<portfolio>:<currency>`` etc. Adding more
bank-specific knobs (custom account hierarchies, fee category names,
locale-specific labels) is now data-only: extend the dataclass and add
or amend the relevant profile.

Adding a new bank is a matter of dropping a new ``Profile`` in
:data:`PROFILES` keyed on the corresponding :class:`~banking_pipeline.models.BankId`
value. Banks not in the registry fall back to :data:`UNKNOWN_PROFILE`,
which produces parseable (if generic-looking) beancount output.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import BankId


@dataclass(frozen=True)
class BankWriterProfile:
    """Per-bank writer configuration.

    Attributes
    ----------
    account_prefix:
        Short account-name segment used as the second component of every
        bank-namespaced beancount account: ``Assets:<account_prefix>:…``,
        ``Expenses:<account_prefix>:…``, ``Income:<account_prefix>:…``.
        Pictet uses ``"Pic"``; the unknown-bank fallback uses ``"Unknown"``.
    withholding_tax_account_template:
        Account for foreign withholding tax split off an income advice,
        formatted with ``country=<ISO 3166-1 alpha-2, upper-cased>``.
        Bank-agnostic by default (``Expenses:Tax:Withholding:{country}``)
        because UK foreign-tax-credit relief is tracked per source
        country, not per custodian; a bank can override the root if it
        ever needs a different hierarchy.
    """

    account_prefix: str
    withholding_tax_account_template: str = "Expenses:Tax:Withholding:{country}"


PICTET_PROFILE = BankWriterProfile(account_prefix="Pic")

UNKNOWN_PROFILE = BankWriterProfile(account_prefix="Unknown")


PROFILES: dict[BankId, BankWriterProfile] = {
    BankId.PICTET: PICTET_PROFILE,
}

# Reverse index from the rendered ``account_prefix`` back to the profile.
# Builders only carry the prefix string (not the originating
# :class:`BankId`), so this lets them recover profile-level knobs like
# ``withholding_tax_account_template`` without threading the whole
# classification through the dispatch signature.
_PROFILE_BY_PREFIX: dict[str, BankWriterProfile] = {
    p.account_prefix: p for p in (*PROFILES.values(), UNKNOWN_PROFILE)
}


def resolve_profile(bank_id: BankId | None) -> BankWriterProfile:
    """Return the profile for ``bank_id``, or :data:`UNKNOWN_PROFILE` if unset.

    The fallback keeps the writer producing parseable beancount even on
    bank-agnostic test fixtures (where the classifier emits ``BankId.UNKNOWN``
    or no bank classification at all).
    """

    if bank_id is None:
        return UNKNOWN_PROFILE
    return PROFILES.get(bank_id, UNKNOWN_PROFILE)


def profile_for_prefix(prefix: str) -> BankWriterProfile:
    """Return the profile whose ``account_prefix`` is ``prefix``.

    Falls back to :data:`UNKNOWN_PROFILE` for an unrecognised prefix.
    """

    return _PROFILE_BY_PREFIX.get(prefix, UNKNOWN_PROFILE)
