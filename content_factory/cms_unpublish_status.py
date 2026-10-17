"""Owner-scoped withdrawal offers and nonce acknowledgements, with no effects."""

from django.core.exceptions import ObjectDoesNotExist

from . import cms_unpublish_authority as authority
from .cms_connection_authority import validate_current
from .cms_owner_connection import _owner
from .cms_unpublish_start import _refresh
from .website_failure_contract import validate_failure


def _projection(original, job, binding):
    result = {"version": 1, "companyId": binding["company_id"], "connectionId": binding["connection_id"],
        "connectionGeneration": binding["generation"], "publicationOperationId": str(original.operation.pk),
        "publicationIntentDigest": original.intent["digest"], "publicUrl": original.state["public_url"], "title": original.intent["title"],
        "operationId": None, "requestId": None, "state": "available", "nextAttemptAt": None, "deadline": None,
        "providerState": "pending", "publicState": "unverified", "checkedAt": None, "failureCode": None}
    if job is None: return result
    value = authority.historical(job)
    if value.intent["publication_operation_id"] != str(original.operation.pk): raise authority.deny()
    allowed = {*authority.ACTIVE, "completed", "needs_user", "blocked", "cancelled"}
    if job.state not in allowed: raise authority.deny()
    state = "paused" if job.state in authority.ACTIVE and not authority.enabled() else job.state
    proof = job.payload["public_observation"]
    if job.state == "completed" and (proof is None or job.receipt.get("status") != "article_withdrawn" or
            job.receipt.get("withdrawal_verified") is not True or job.receipt.get("publicProofDigest") != proof["digest"]):
        raise authority.deny()
    try: failure = validate_failure(job.receipt.get("failure"))["code"]
    except (ValueError, TypeError, KeyError): failure = None
    result.update(operationId=str(job.pk), requestId=value.intent["request_id"], state=state,
        nextAttemptAt=job.next_attempt_at.isoformat() if state in authority.ACTIVE and job.next_attempt_at is not None else None,
        deadline=value.deadline.isoformat(), providerState="draft" if value.journal["phase"] == "provider_draft" else
            "unknown" if value.journal["outcome_unknown"] else "pending", publicState="withdrawn" if proof is not None else "unverified",
        checkedAt=proof["checked_at"] if proof is not None else None, failureCode=failure)
    return result


def packet(context, config, *, user, publication_id=None, request_id=None):
    """Read actual owner metadata or the original nonce; never replay a POST."""
    from .website_models import WebsiteConnectionOperation
    try:
        _refresh(context, config, user)
        _owner(context, config, user)
        binding = validate_current(context, config, config.website_connection, user=user)
        job = None
        if request_id is not None:
            authority.contract._uuid(request_id)
            job = WebsiteConnectionOperation.objects.filter(action=authority.ACTION,
                idempotency_key=f"cms-unpublish:{binding['connection_id']}:{binding['generation']}:{request_id}").first()
            if job is None:
                # Recheck the real owner even for an absent acknowledgement.
                _refresh(context, config, user)
                if validate_current(context, config, config.website_connection, user=user) != binding: raise authority.deny()
                return {"cmsWithdrawal": None}
            selected = authority.historical(job)
            if publication_id is not None and publication_id != selected.intent["publication_operation_id"]: raise authority.deny()
            publication_id = selected.intent["publication_operation_id"]
        authority.contract._uuid(publication_id)
        original = authority.target(WebsiteConnectionOperation.objects.filter(pk=publication_id, action="cms-publish").first(), binding=binding)
        if job is None:
            job = WebsiteConnectionOperation.objects.filter(action=authority.ACTION, connection_id=binding["connection_id"],
                generation=binding["generation"], payload__intent__publication_operation_id=publication_id).order_by("-created_at", "-pk").first()
        result = _projection(original, job, binding) if job is not None or authority.enabled() else None
        _refresh(context, config, user)
        if validate_current(context, config, config.website_connection, user=user) != binding: raise authority.deny()
        return {"cmsWithdrawal": result}
    except (ObjectDoesNotExist, authority.contract.CmsApiError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise authority.deny() from None


def apply(result, operation, context, config, *, user):
    """Separate a withdrawal result from the immutable publication checklist."""
    from .website_models import WebsiteConnectionOperation
    source = operation.payload.get("source_run_id")
    if not source: return result
    publication = WebsiteConnectionOperation.objects.filter(action="cms-publish", connection_id=operation.connection_id,
        generation=operation.generation, payload__intent__source_run_id=source).order_by("-created_at", "-pk").first()
    if publication is None: return result
    try: selected = packet(context, config, user=user, publication_id=str(publication.pk))["cmsWithdrawal"]
    except authority.WebsiteAuthorityError: return result
    if selected is None: return result
    result["cmsWithdrawal"] = selected
    if selected["state"] == "available": return result
    result.pop("publicUrl", None)
    result.update(action=None, nextAttemptAt=selected["nextAttemptAt"])
    if selected["state"] == "completed":
        result.update(state="done", title="Article unpublished", message="The article was returned to a draft. The checklist records its earlier publication.")
    elif selected["state"] in authority.ACTIVE:
        result.update(state="working", title="Unpublishing article", message="Checking the original article and its public URL.")
    else:
        result.update(state="stopped", title="Article withdrawal needs attention", message="Review the original withdrawal request.",
            action={"id": "inspect_goal", "label": "View details", "operationId": selected["operationId"]})
    return result
