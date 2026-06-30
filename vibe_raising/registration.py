"""The single chokepoint that gates vibe-raising to registered Australian companies.

Every path that marks a founder company as ``registered`` must route through
:func:`verify_and_persist_company_registration`. It runs three layers of checks —
ABN checksum, an authoritative Australian Business Register (ABR) lookup, and the ACN
checksum — and only flips ``registered`` (and stamps ``abr_verified_at``) when the
entity is an *active registered company*. Anything else raises
:class:`CompanyRegistrationError`, which callers translate to a structured HTTP 422.
"""

from __future__ import annotations

import re

from django.conf import settings
from django.utils import timezone

from vibe_raising.validators import (
    acn_from_abn,
    normalize_abn,
    normalize_acn,
    validate_abn_checksum,
    validate_acn_checksum,
)

# Error codes surfaced to the client. Keep these stable — the frontend maps them to
# user-facing copy.
ABN_REQUIRED = "ABN_REQUIRED"
ABN_INVALID = "ABN_INVALID"
ACN_REQUIRED = "ACN_REQUIRED"
ACN_INVALID = "ACN_INVALID"
ACN_MISMATCH = "ACN_MISMATCH"
NOT_A_REGISTERED_COMPANY = "NOT_A_REGISTERED_COMPANY"
ABR_UNVERIFIABLE = "ABR_UNVERIFIABLE"

_DEFAULT_MESSAGES = {
    ABN_REQUIRED: "Add your ABN before continuing.",
    ABN_INVALID: "That ABN doesn't look right — check the digits.",
    ACN_REQUIRED: "We couldn't determine the ACN for this company.",
    ACN_INVALID: "That ACN doesn't look right — check the digits.",
    ACN_MISMATCH: "The ACN doesn't match this ABN. Check the details and try again.",
    NOT_A_REGISTERED_COMPANY: (
        "Vibe-raising is for registered Australian companies (Pty Ltd / Ltd). "
        "We couldn't find a company registered to this ABN."
    ),
    ABR_UNVERIFIABLE: (
        "We couldn't verify with the Australian Business Register just now. "
        "Please try again in a moment."
    ),
}

_ACN_FIELD_CODES = {ACN_REQUIRED, ACN_INVALID, ACN_MISMATCH}


class CompanyRegistrationError(Exception):
    """Raised when a company fails the registered-Australian-company gate."""

    def __init__(self, code: str, message: str | None = None, field: str | None = None):
        self.code = code
        self.message = message or _DEFAULT_MESSAGES.get(code, "Company verification failed.")
        self.field = field or ("acn" if code in _ACN_FIELD_CODES else "abn")
        super().__init__(self.message)

    def to_payload(self) -> dict:
        """Structured body for an HTTP 422 response."""

        return {"code": self.code, "detail": self.message, "field": self.field}


# Fields cleared when a verification is dropped, so callers using update_fields can
# persist the reset in one place.
REGISTRATION_FIELDS = ("acn", "entity_type_code", "abr_verified_at", "registered")


def invalidate_company_registration(company) -> None:
    """Drop a company's verification (does not save).

    Used when an ABN is changed outside the verification path so a stale ``registered``
    flag / ACN can never outlive the ABN it was verified against.
    """

    company.acn = None
    company.entity_type_code = ""
    company.abr_verified_at = None
    company.registered = False


def set_unverified_company_abn(company, abn) -> None:
    """Store a not-yet-verified ABN (does not save).

    If the company was previously verified and the ABN actually changes, the prior
    verification is invalidated first. Idempotent re-saves of the same ABN are a no-op.
    """

    new_value = (str(abn).strip() or None) if abn is not None else None
    if company.abr_verified_at is not None and normalize_abn(new_value) != normalize_abn(company.abn):
        invalidate_company_registration(company)
    company.abn = new_value


def company_is_verified(company) -> bool:
    """True when a company is a confirmed registered Australian company."""

    return bool(
        company is not None
        and company.registered
        and company.acn
        and company.abr_verified_at
    )


def company_registration_blocker(company) -> dict | None:
    """Return a structured 422 body when ``company`` may not proceed to an update.

    ``None`` means the company is verified and the caller may continue.
    """

    if company_is_verified(company):
        return None
    return {
        "code": ACN_REQUIRED,
        "detail": (
            "Verify your company's ACN as a registered Australian company "
            "before creating an update."
        ),
        "field": "abn",
        "redirect": "/founder-tools/company-setup",
    }


def _abr_verifier():
    # Imported lazily: the ABR helper lives in the large vibe-marketing views module,
    # and a lazy import keeps this service layer cheap to import and free of load-order
    # coupling to that module.
    from content_factory.vibe_marketing_views import verify_company_with_abr

    return verify_company_with_abr


def verify_and_persist_company_registration(
    company,
    *,
    abn,
    acn=None,
    save: bool = True,
    abr_verifier=None,
):
    """Verify ``company`` is an active registered Australian company and persist it.

    On success, mutates ``company`` (``abn``, ``acn``, ``entity_type_code``,
    ``abr_verified_at``, ``registered=True``) and writes the row when ``save`` is True.
    Raises :class:`CompanyRegistrationError` on any failure, leaving ``company``
    unmodified.

    ``abr_verifier`` is injectable for tests; it defaults to the live ABR lookup.
    """

    # --- Layer 1: ABN presence + checksum -------------------------------------
    raw_digits = re.sub(r"\D", "", str(abn or ""))
    if not raw_digits:
        raise CompanyRegistrationError(ABN_REQUIRED)
    if not validate_abn_checksum(abn):
        raise CompanyRegistrationError(ABN_INVALID)
    normalized_abn = normalize_abn(abn)

    # --- Layer 2: authoritative ABR company check -----------------------------
    skip_abr = bool(getattr(settings, "VIBE_RAISING_SKIP_ABR_VERIFICATION", False))
    if skip_abr:
        # Dev/local escape hatch when no ABR credentials are configured: trust the ABN
        # and derive the ACN, but still enforce both checksums below.
        abr = {
            "reachable": True,
            "found": True,
            "is_company": True,
            "acn": None,
            "entity_type_code": "",
        }
    else:
        verifier = abr_verifier or _abr_verifier()
        abr = verifier(normalized_abn)
        if not abr.get("reachable") or not abr.get("configured", True):
            raise CompanyRegistrationError(ABR_UNVERIFIABLE)
        if not abr.get("found") or not abr.get("is_company"):
            raise CompanyRegistrationError(NOT_A_REGISTERED_COMPANY)

    # --- Layer 3: resolve + validate the ACN ----------------------------------
    abr_acn = normalize_acn(abr.get("acn"))
    derived_acn = acn_from_abn(normalized_abn)
    supplied_acn = normalize_acn(acn) if acn else None

    resolved_acn = abr_acn or derived_acn
    if not resolved_acn:
        raise CompanyRegistrationError(ACN_REQUIRED)

    # Every ACN we can resolve must agree — a mismatch means the inputs are inconsistent.
    for candidate in (derived_acn, supplied_acn):
        if candidate and candidate != resolved_acn:
            raise CompanyRegistrationError(ACN_MISMATCH)

    if not validate_acn_checksum(resolved_acn):
        raise CompanyRegistrationError(ACN_INVALID)

    # --- Persist --------------------------------------------------------------
    company.abn = normalized_abn
    company.acn = resolved_acn
    company.entity_type_code = abr.get("entity_type_code") or ""
    company.abr_verified_at = timezone.now()
    company.registered = True
    if save:
        company.save()

    return abr
