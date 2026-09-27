"""Verify Australian startup ABNs before granting founder benefits.

An active ABR registration is required; incorporation as a company is not. This
includes not-for-profits such as incorporated associations without an ACN.
"""

from __future__ import annotations

from django.utils import timezone

from vibe_raising.validators import (
    acn_from_abn,
    normalize_abn,
    normalize_acn,
    is_registered_company_entity_type,
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
        "We couldn't find an active Australian business registration for this ABN. "
        "Not-for-profits and incorporated associations can qualify without an ACN."
    ),
    ABR_UNVERIFIABLE: (
        "We couldn't verify with the Australian Business Register just now. "
        "Please try again in a moment."
    ),
}

_ACN_FIELD_CODES = {ACN_REQUIRED, ACN_INVALID, ACN_MISMATCH}


class CompanyRegistrationError(Exception):
    """Raised when a startup's ABR registration cannot be verified."""

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
    """True when a startup has a valid ABN and a successful ABR verification."""

    return bool(
        company is not None
        and getattr(company, "registered", False)
        and validate_abn_checksum(getattr(company, "abn", None))
        and str(getattr(company, "entity_type_code", "") or "").strip()
        and getattr(company, "abr_verified_at", None)
    )


def company_registration_status(company) -> dict:
    """Expose verification status and the latest save's safe validation error."""

    if company_is_verified(company):
        return {"verified": True, "code": None, "detail": None, "field": None}
    error = getattr(company, "_registration_error", None)
    if error is None:
        code = ABN_REQUIRED if not getattr(company, "abn", None) else ABR_UNVERIFIABLE
        error = CompanyRegistrationError(code).to_payload()
    return {"verified": False, **error}


def company_registration_blocker(company) -> dict | None:
    """Return a structured 422 body when ``company`` may not proceed to an update.

    ``None`` means the company is verified and the caller may continue.
    """

    if company_is_verified(company):
        return None
    return {
        "code": ABN_REQUIRED,
        "detail": (
            "Verify your startup's active Australian ABN "
            "before creating an update."
        ),
        "field": "abn",
        "redirect": "/founder-tools/company-setup",
    }


def attempt_company_verification(company, *, abn=None, acn=None, save: bool = True) -> bool:
    """Best-effort verification: stamp the company as verified when its ABN/ACN check
    out, otherwise leave it usable and unverified.

    Used where being a verified company *unlocks perks* (e.g. the coworking discount)
    but is not required to use the product — so an invalid or missing ABN must not block
    the founder. Returns ``True`` when the company is now verified.
    """

    target_abn = abn if abn is not None else company.abn
    company._registration_error = None
    try:
        verify_and_persist_company_registration(
            company, abn=target_abn, acn=acn, save=save
        )
        return True
    except CompanyRegistrationError as exc:
        # Not verifiable — drop any stale verification but keep the company as-is.
        invalidate_company_registration(company)
        company._registration_error = exc.to_payload()
        if save and getattr(company, "pk", None):
            company.save(update_fields=[*REGISTRATION_FIELDS, "updated_at"])
        return False


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
    """Verify ``company`` has an active Australian ABN and persist the result.

    On success, mutates ``company`` (``abn``, ``acn``, ``entity_type_code``,
    ``abr_verified_at``, ``registered=True``) and writes the row when ``save`` is True.
    Raises :class:`CompanyRegistrationError` on any failure, leaving ``company``
    unmodified.

    ``abr_verifier`` is injectable for tests; it defaults to the live ABR lookup.
    """

    # --- Layer 1: ABN presence + checksum -------------------------------------
    has_abn = bool(str(abn or "").strip())
    has_acn = bool(str(acn or "").strip())
    if not has_abn and not has_acn:
        raise CompanyRegistrationError(ABN_REQUIRED)
    if has_abn and not validate_abn_checksum(abn):
        raise CompanyRegistrationError(ABN_INVALID)
    if has_acn and not validate_acn_checksum(acn):
        raise CompanyRegistrationError(ACN_INVALID)
    normalized_abn = normalize_abn(abn)
    supplied_acn = normalize_acn(acn)

    # --- Layer 2: authoritative ABR registration check ------------------------
    # No configuration flag may manufacture a verification timestamp. Tests can
    # inject the verifier; benefits always require a successful register lookup.
    verifier = abr_verifier or _abr_verifier()
    try:
        abr = verifier(normalized_abn or supplied_acn)
    except Exception as exc:
        raise CompanyRegistrationError(ABR_UNVERIFIABLE) from exc
    if not isinstance(abr, dict) or not abr.get("reachable") or not abr.get("configured"):
        raise CompanyRegistrationError(ABR_UNVERIFIABLE)
    resolved_abn = normalize_abn(abr.get("abn"))
    if (
        not abr.get("found")
        or not abr.get("active")
        or not str(abr.get("entity_type_code") or "").strip()
        or not validate_abn_checksum(resolved_abn)
        or (normalized_abn and resolved_abn != normalized_abn)
    ):
        raise CompanyRegistrationError(NOT_A_REGISTERED_COMPANY)
    normalized_abn = resolved_abn

    # --- Layer 3: resolve + validate the ACN ----------------------------------
    # ASICNumber may also describe an ARBN/ARSN, so only treat it as an ACN for
    # Australian company entity types. Associations do not need a company ACN.
    resolved_acn = None
    if is_registered_company_entity_type(abr.get("entity_type_code")):
        raw_abr_acn = abr.get("acn")
        if raw_abr_acn and not validate_acn_checksum(raw_abr_acn):
            raise CompanyRegistrationError(ACN_INVALID)
        derived_acn = acn_from_abn(normalized_abn)
        resolved_acn = normalize_acn(raw_abr_acn) or derived_acn
        if not resolved_acn or not validate_acn_checksum(resolved_acn):
            raise CompanyRegistrationError(ACN_INVALID)
        if derived_acn != resolved_acn:
            raise CompanyRegistrationError(ACN_MISMATCH)
    if supplied_acn and supplied_acn != resolved_acn:
        raise CompanyRegistrationError(ACN_MISMATCH)

    # --- Persist --------------------------------------------------------------
    company.abn = normalized_abn
    company.acn = resolved_acn
    company.entity_type_code = abr.get("entity_type_code") or ""
    company.abr_verified_at = timezone.now()
    company.registered = True
    company._registration_error = None
    if save:
        company.save()

    return abr
