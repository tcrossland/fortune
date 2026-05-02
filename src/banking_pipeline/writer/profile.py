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
    """

    account_prefix: str


PICTET_PROFILE = BankWriterProfile(account_prefix="Pic")

UNKNOWN_PROFILE = BankWriterProfile(account_prefix="Unknown")


PROFILES: dict[BankId, BankWriterProfile] = {
    BankId.PICTET: PICTET_PROFILE,
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
