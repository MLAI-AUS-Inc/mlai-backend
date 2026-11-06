"""Purpose-bound staff authority for one portable, already funded continuation."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from django.db import transaction

from .run_ownership import (STAFF_SUPPORT_RESULT_KEY, RunOwnershipConflict, current_execution_ticket,
    owner_receipt, user_principal, user_stop_intent, wire_identity)
from .website_contract import CONNECTION_FIELDS, evidence_digest

_issued_action = ContextVar("staff_portable_action", default=None)


def _current_staff(user_id):
    from core.models import User
    from community_chat.admin_access import AdminError
    from .staff_console import require_staff
    user = User.objects.filter(pk=user_id, is_active=True).first()
    try:
        if user is None:
            raise RunOwnershipConflict("The requesting staff account is no longer active.")
        require_staff(user)
    except AdminError as exc:
        raise RunOwnershipConflict("The requesting staff permission changed.") from exc
    return user


def staff_support_required(source):
    from .run_ownership import admission_enabled
    from workflow_runs.models import ContentFactoryRun
    return admission_enabled() or owner_receipt(source) is not None or ContentFactoryRun.objects.filter(
        organization_id=source.organization_id,
        result__run_owner_staff_support_v1__source__source_pk=source.pk).exists()


def _source_proof(source, company):
    from founder_tools import startup_team
    from .billing_lineage import original_article_spend_for_run, paid_article_request
    from .first_article_entitlement import entitlement_payload, entitled_request
    if (source.organization_id != company.organization_id or source.domain != company.organization.domain
            or company.profile.role != "founder" or not company.profile.user.is_active
            or startup_team._owner(source.organization) != company.profile.user_id
            or user_stop_intent(source) or source.status in {"cancelled", "denied"}):
        raise RunOwnershipConflict("The original article or current startup owner changed.")
    with startup_team._locked(source.organization) as (_, roster):
        if roster and roster.get("frozen"):
            raise RunOwnershipConflict("The startup is currently frozen.")
    request = source.run_request or {}
    spend = original_article_spend_for_run(source) if paid_article_request(request) else None
    if spend is not None:
        if spend.service_refunds.exists():
            raise RunOwnershipConflict("The original article payment was refunded.")
        billing = {"kind": "paid", "ledger_id": spend.pk, "payer_id": spend.user_id,
            "key": spend.idempotency_key, "delta": spend.delta_microroo}
    elif entitled_request(request):
        entitlement = entitlement_payload(source)
        if entitlement.get("entitlementStatus") not in {"reserved", "consumed"}:
            raise RunOwnershipConflict("The original article benefit is no longer available.")
        billing = {"kind": "entitled", "ledger_id": str(request.get("roo_points_ledger_id")),
            "grant_available": True}
    else:
        raise RunOwnershipConflict("The source has no carried article payment or benefit.")
    receipt = owner_receipt(source)
    return {"source_pk": source.pk, "source_run_id": source.run_id,
        "source_request_sha256": evidence_digest(request),
        "source_admission": wire_identity(receipt) if receipt is not None else None,
        "source_execution": current_execution_ticket(source) if receipt is not None else None,
        "company_id": str(company.pk), "owner_id": str(company.profile.user_id), "billing": billing}


@contextmanager
def staff_action_authority(source, *, user, command_key):
    """Entered only by the claimed, authenticated staff action service."""
    from founder_tools.models import VibeRaisingCompany
    from workflow_runs.models import ContentFactoryRun
    from organizations.models import Organization
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=source.organization_id)
        current = ContentFactoryRun.objects.select_for_update().get(pk=source.pk)
        staff = _current_staff(user.pk)
        company = VibeRaisingCompany.objects.filter(organization_id=current.organization_id).select_related(
            "profile__user", "organization").order_by("created_at", "pk").first()
        if company is None:
            raise RunOwnershipConflict("This startup has no current owning founder.")
        issued = {"user_id": staff.pk, "command_sha256": evidence_digest({"key": command_key,
            "user": staff.pk, "organization": current.organization_id, "action": "switch_to_draft"}),
            "source": _source_proof(current, company)}
    token = _issued_action.set(issued)
    try:
        yield
    finally:
        _issued_action.reset(token)


def issued_staff_user():
    issued = _issued_action.get()
    if issued is None:
        raise RunOwnershipConflict("A claimed staff recovery action is required.")
    return _current_staff(issued["user_id"])


def seal_staff_support(child, *, source, company):
    """Bind private support authority after charge carry and immutable admission."""
    from founder_tools.services import founder_actor_id_for_user
    from workflow_runs.models import ContentFactoryRun
    if not transaction.get_connection().in_atomic_block:
        raise RunOwnershipConflict("Staff recovery must share the original reservation transaction.")
    issued, staff = _issued_action.get(), issued_staff_user()
    proof = _source_proof(source, company)
    if proof != issued["source"]:
        raise RunOwnershipConflict("The original source changed before staff recovery.")
    principal = user_principal(staff.pk, founder_actor_id_for_user(staff))
    if owner_receipt(child)["identity"]["principal"] != principal:
        raise RunOwnershipConflict("Staff recovery must retain its actual requesting user.")
    receipt = {"schema_version": 1, "purpose": "portable_carried_article", "action": "switch_to_draft",
        "organization_id": str(child.organization_id), "principal": principal,
        "command_sha256": issued["command_sha256"], "source": proof,
        "child_admission": wire_identity(owner_receipt(child))}
    child.result = {**child.result, STAFF_SUPPORT_RESULT_KEY: receipt}
    ContentFactoryRun.objects.filter(pk=child.pk).update(result=child.result)
    return deepcopy(receipt)


def require_staff_support(run):
    """Recheck this exact private support grant before dispatch or control."""
    from founder_tools.models import VibeRaisingCompany
    from .website_operations import OPERATION_FIELDS
    from workflow_runs.models import ContentFactoryRun
    from organizations.models import Organization
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=run.organization_id)
        child = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        saved = (child.result or {}).get(STAFF_SUPPORT_RESULT_KEY)
        admission = owner_receipt(child)
        if (not isinstance(saved, dict) or saved.get("schema_version") != 1
                or saved.get("purpose") != "portable_carried_article" or saved.get("action") != "switch_to_draft"
                or saved.get("organization_id") != str(child.organization_id) or admission is None
                or saved.get("child_admission") != wire_identity(admission)
                or saved.get("principal") != admission["identity"]["principal"] or user_stop_intent(child)):
            raise RunOwnershipConflict("The saved staff recovery authority changed.")
        request = child.run_request
        if (child.workflow != "article_generation" or child.github_repo or request.get("github_repo")
                or request.get("delivery_mode") != "content_only" or request.get("delivery_mode_confirmed") is not True
                or any(request.get(key) not in (None, "") for key in (*CONNECTION_FIELDS, *OPERATION_FIELDS))
                or str(request.get("roo_points_requested_by_user_id")) != saved["principal"]["user_id"]):
            raise RunOwnershipConflict("Staff recovery authorizes only the carried portable article.")
        staff = _current_staff(saved["principal"]["user_id"])
        proof = saved.get("source")
        if not isinstance(proof, dict):
            raise RunOwnershipConflict("The staff recovery source is unavailable.")
        source = ContentFactoryRun.objects.select_for_update().filter(pk=proof.get("source_pk"),
            organization_id=child.organization_id).first()
        company = VibeRaisingCompany.objects.filter(pk=proof.get("company_id"),
            organization_id=child.organization_id).select_related("profile__user", "organization").first()
        if (source is None or company is None or request.get("staff_switch_source_pk") != source.pk
                or _source_proof(source, company) != proof):
            raise RunOwnershipConflict("The current staff recovery source changed.")
        original = source.run_request
        if (request.get("original_billing_source_run_id") != (original.get("original_billing_source_run_id") or source.run_id)
                or any(request.get(key) != value for key, value in original.items()
                    if key.startswith("roo_points_") and key != "roo_points_requested_by_user_id")):
            raise RunOwnershipConflict("The carried original payment changed.")
        return staff
