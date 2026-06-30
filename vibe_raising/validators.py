"""Validation helpers for gating vibe-raising to registered Australian companies.

A vibe-raising founder company must be a *registered Australian company* (Pty Ltd /
Ltd) before it can proceed to investor updates. The gate hangs off the ACN:

* An **ABN** (11 digits) can belong to any entity — sole trader, trust, partnership
  or company.
* An **ACN** (9 digits) is issued by ASIC *only* to registered companies. Requiring a
  valid ACN is therefore the company gate.
* A company's ABN is mathematically ``2 check digits + its 9-digit ACN``, so for a
  company the ACN can be derived from the ABN — see :func:`acn_from_abn`.

These helpers are intentionally dependency-free (no Django, no network) so they can be
unit-tested in isolation and reused from every save path. The authoritative
"is this an active, registered company" decision additionally relies on an Australian
Business Register (ABR) lookup; the entity-type helpers here support that check but do
not replace it.
"""

from __future__ import annotations

import re

# ABN check uses a modulus-89 weighted sum (ATO algorithm).
_ABN_WEIGHTS = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)

# ACN check uses a modulus-10 weighted sum over the first eight digits (ASIC algorithm).
_ACN_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 1)

# ABR ``entityTypeCode`` values that correspond to ASIC-registered companies. This is a
# *supporting* signal — the primary signal that an entity is a company is that the ABR
# record carries an ACN (``ASICNumber``). Keep this list conservative and confirm
# against the ABR entity-type reference before widening it, since a missing code here
# hard-blocks a legitimate company.
COMPANY_ENTITY_TYPE_CODES = frozenset(
    {
        "PRV",  # Australian Private Company (Pty Ltd)
        "PUB",  # Australian Public Company (Ltd)
    }
)

# Friendly names for the company entity-type codes, surfaced to the UI so the frontend
# doesn't have to hardcode the mapping.
ENTITY_TYPE_NAMES = {
    "PRV": "Australian Private Company",
    "PUB": "Australian Public Company",
}


def entity_type_display(entity_type_code: str | None) -> str:
    """Human-readable name for an ABR entity-type code; falls back to the code itself."""

    code = (entity_type_code or "").strip().upper()
    return ENTITY_TYPE_NAMES.get(code, code)


# ABR ``entityTypeCode`` values for registered not-for-profit organisations that have
# an ABN but no ACN (incorporated associations, co-operatives, etc.). These are exempt
# from the ACN requirement. Keep conservative and confirm against the ABR entity-type
# reference before widening — and note this only auto-classifies when the ABR lookup is
# available; otherwise the manual ``is_nonprofit`` flag is the signal.
NONPROFIT_ENTITY_TYPE_CODES = frozenset(
    {
        "OIE",  # Other Incorporated Entity (incorporated associations)
        "COP",  # Co-operative
    }
)


def is_nonprofit_entity_type(entity_type_code: str | None) -> bool:
    """Return ``True`` when an ABR ``entityTypeCode`` denotes a registered not-for-profit."""

    if not entity_type_code:
        return False
    return entity_type_code.strip().upper() in NONPROFIT_ENTITY_TYPE_CODES


def _digits(value: str | None) -> str:
    """Return only the decimal digits in ``value`` (drops spaces, hyphens, etc.)."""

    if not value:
        return ""
    return re.sub(r"\D", "", str(value))


def validate_abn_checksum(value: str | None) -> bool:
    """Return ``True`` when ``value`` is a checksum-valid 11-digit ABN.

    Algorithm (ATO): subtract 1 from the leading digit, take the weighted sum against
    :data:`_ABN_WEIGHTS`, and require the total to be divisible by 89.
    """

    digits = _digits(value)
    if len(digits) != 11:
        return False
    nums = [int(ch) for ch in digits]
    nums[0] -= 1
    total = sum(num * weight for num, weight in zip(nums, _ABN_WEIGHTS))
    return total % 89 == 0


def validate_acn_checksum(value: str | None) -> bool:
    """Return ``True`` when ``value`` is a checksum-valid 9-digit ACN.

    Algorithm (ASIC): weight the first eight digits against :data:`_ACN_WEIGHTS`, take
    the complement of the modulus-10 remainder, and compare it to the ninth digit. A
    remainder of 0 yields a check digit of 0 (``(10 - 0) % 10``).
    """

    digits = _digits(value)
    if len(digits) != 9:
        return False
    nums = [int(ch) for ch in digits]
    total = sum(num * weight for num, weight in zip(nums[:8], _ACN_WEIGHTS))
    check = (10 - (total % 10)) % 10
    return check == nums[8]


def acn_from_abn(value: str | None) -> str | None:
    """Derive the 9-digit ACN embedded in a company's 11-digit ABN.

    A company ABN is its ACN prefixed with two check digits, so the ACN is the trailing
    nine digits. Returns ``None`` when ``value`` is not 11 digits. The result is *not*
    guaranteed to be a valid ACN (the caller should still run
    :func:`validate_acn_checksum`) — for a non-company ABN the trailing digits are
    meaningless.
    """

    digits = _digits(value)
    if len(digits) != 11:
        return None
    return digits[2:]


def normalize_abn(value: str | None) -> str | None:
    """Return the bare 11-digit ABN, or ``None`` when not 11 digits."""

    digits = _digits(value)
    return digits if len(digits) == 11 else None


def normalize_acn(value: str | None) -> str | None:
    """Return the bare 9-digit ACN, or ``None`` when not 9 digits."""

    digits = _digits(value)
    return digits if len(digits) == 9 else None


def format_acn(value: str | None) -> str:
    """Format a 9-digit ACN as ``XXX XXX XXX``; pass other values through trimmed."""

    digits = _digits(value)
    if len(digits) == 9:
        return f"{digits[:3]} {digits[3:6]} {digits[6:]}"
    return (value or "").strip()


def is_registered_company_entity_type(entity_type_code: str | None) -> bool:
    """Return ``True`` when an ABR ``entityTypeCode`` denotes a registered company."""

    if not entity_type_code:
        return False
    return entity_type_code.strip().upper() in COMPANY_ENTITY_TYPE_CODES
