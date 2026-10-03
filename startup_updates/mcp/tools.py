"""Startup-scoped narrative tools. Imported assertions never become financial evidence."""
from __future__ import annotations

import copy
import re
from datetime import date, timedelta
from urllib.parse import urlencode, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import ValidationError

from startup_updates.evidence_contract import content_hash, reporting_period
from startup_updates.models import MonthlyUpdateDraft, StartupProfile
from startup_updates.revisions import RevisionConflict, capture_snapshot, save_revision
from startup_updates.update_identity import resolve_update
from .oauth import company_for, valid_grant

NARRATIVE_FIELDS = {"summary": "summary", "highlights": "highlights", "challenges": "lowlights",
    "learnings": "learnings", "next30Days": "next_30_days", "asks": "asks"}
SOURCE_FIELDS = frozenset({"provider", "url", "title", "occurredAt"})
SAVE_FIELDS = frozenset({"companyId", "month", "requestId", "updateId", "expectedRevision", "narrative", "sources", "coverageNotes"})


def object_schema(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


COMPANY = {"type": "string", "format": "uuid", "description": "Startup company UUID from list_startups."}
MONTH = {"type": "string", "pattern": r"^\d{4}-\d{2}$", "description": "Calendar reporting month YYYY-MM."}
TOOLS = [
    {"name": "list_startups", "title": "List authorised startups", "description": "List startups this account has explicitly authorised this agent to access.",
        "inputSchema": object_schema({}, []), "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    {"name": "get_monthly_update_brief", "title": "Get monthly update brief", "description": "Get startup context and reporting dates. Search the user's already-connected sources for these dates; save a narrative draft with citations. Financial figures come from Valley's direct connections.",
        "inputSchema": object_schema({"companyId": COMPANY, "month": MONTH}, ["companyId", "month"]), "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    {"name": "save_narrative_draft", "title": "Save a private narrative draft", "description": "Save a private monthly update for founder review. Only narrative and source references are accepted. Financial metrics and publishing cannot be changed. Retry using the same requestId; existing draft edits require expectedRevision.",
        "inputSchema": object_schema({"companyId": COMPANY, "month": MONTH, "requestId": {"type": "string", "format": "uuid"},
            "updateId": {"type": "integer", "minimum": 1}, "expectedRevision": {"type": ["integer", "null"]},
            "narrative": object_schema({field: {"type": "string", "maxLength": 20000} for field in NARRATIVE_FIELDS}, ["summary"]),
            "sources": {"type": "array", "maxItems": 100, "items": object_schema({field: {"type": "string"} for field in SOURCE_FIELDS}, ["provider", "title"])},
            "coverageNotes": {"type": "string", "maxLength": 10000}}, ["companyId", "month", "requestId", "narrative"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    {"name": "get_draft_status", "title": "Check draft status", "description": "Check the saved revision and open its private review link.",
        "inputSchema": object_schema({"companyId": COMPANY, "updateId": {"type": "integer", "minimum": 1}}, ["companyId", "updateId"]),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
]


def month_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ValidationError("Use a calendar month in YYYY-MM format.")
    try:
        result = date.fromisoformat(value + "-01")
    except ValueError as exc:
        raise ValidationError("Use a valid reporting month.") from exc
    if not 2000 <= result.year <= 2100:
        raise ValidationError("Use a reporting year between 2000 and 2100.")
    return result


def validate_save(arguments):
    if not isinstance(arguments, dict) or set(arguments) - SAVE_FIELDS:
        raise ValidationError("Only narrative, source references and draft identity can be submitted. Financial fields are read-only.")
    for field in ("companyId", "month", "requestId", "narrative"):
        if field not in arguments:
            raise ValidationError(f"{field} is required.")
    month = month_date(arguments["month"])
    try:
        request_id = str(UUID(str(arguments["requestId"])))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("Use a UUID requestId for retry protection.") from exc
    narrative = arguments["narrative"]
    if not isinstance(narrative, dict) or set(narrative) - NARRATIVE_FIELDS.keys() or not narrative.get("summary"):
        raise ValidationError("Provide a summary and supported narrative sections only.")
    for value in narrative.values():
        if not isinstance(value, str) or len(value) > 20000:
            raise ValidationError("Narrative sections must be text of at most 20,000 characters.")
    sources = arguments.get("sources", [])
    if not isinstance(sources, list) or len(sources) > 100:
        raise ValidationError("Provide at most 100 source references.")
    for source in sources:
        if not isinstance(source, dict) or set(source) - SOURCE_FIELDS or not source.get("provider") or not source.get("title"):
            raise ValidationError("Each source needs a provider, title, and supported source fields.")
        if any(not isinstance(value, str) or len(value) > 2048 for value in source.values()):
            raise ValidationError("Source references must be short text values.")
        if source.get("url"):
            try:
                parsed = urlsplit(source["url"])
                parsed.port
            except ValueError as exc:
                raise ValidationError("Use a valid source URL.") from exc
            if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password:
                raise ValidationError("Source links must use HTTP or HTTPS without embedded credentials.")
        if source.get("occurredAt"):
            timestamp = parse_datetime(source["occurredAt"])
            if timestamp is None or timezone.is_naive(timestamp):
                raise ValidationError("Source dates need an explicit timezone.")
    notes = arguments.get("coverageNotes", "")
    if not isinstance(notes, str) or len(notes) > 10000:
        raise ValidationError("Coverage notes must be text of at most 10,000 characters.")
    for field in ("updateId", "expectedRevision"):
        value = arguments.get(field)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            raise ValidationError(f"{field} must be a positive revision identifier.")
    return month, request_id


def status_payload(company, draft):
    revision = draft.current_revision
    query = urlencode({"company_id": company.pk, "view": "compose", "update_id": draft.pk})
    return {"updateId": draft.pk, "companyId": str(company.pk), "month": draft.month.isoformat()[:7],
        "status": draft.status, "revisionId": revision.pk if revision else None,
        "revisionHash": revision.content_hash if revision else None,
        "requiresReview": not revision or getattr(draft, "published_revision_id", None) != revision.pk,
        "provenance": (revision.structured_memo.get("_agent_provenance") or {}).get("kind") if revision else None,
        "reviewUrl": settings.COMMUNITY_CHAT_FRONTEND_URL.rstrip("/") + "/my-startup/updates?" + query}


def _brief(company, month):
    profile = StartupProfile.objects.filter(organization=company.organization).first()
    zone = getattr(profile, "reporting_timezone", "UTC") or "UTC"
    if month > timezone.now().astimezone(ZoneInfo(zone)).date().replace(day=1):
        raise ValidationError("Choose the current month or an earlier month.")
    period = reporting_period(month, zone)
    return {"companyId": str(company.pk), "startup": {"name": company.name, "domain": company.organization.domain,
        "description": getattr(profile, "short_description", ""), "stage": getattr(profile, "stage", "")},
        "month": month.isoformat()[:7], "reportingPeriod": period,
        "sections": list(NARRATIVE_FIELDS), "instructions": [
            "Use the user's already-connected Gmail, Linear, Luma or other relevant sources within reportingPeriod.",
            "Distinguish completed work from plans; include source links and dated evidence when available.",
            "Describe unavailable sources and incomplete coverage in coverageNotes; exclude unrelated personal information.",
            "Leave financial figures to Valley's direct Xero/Stripe evidence. Do not submit financial metric fields.",
            "Save a private draft using save_narrative_draft and return its reviewUrl. The founder reviews and publishes in MLAI Chat."]}


@transaction.atomic
def save_draft(principal, arguments):
    month, request_id = validate_save(arguments)
    # Account row lock serialises against revocation/deletion paths. Recheck
    # permissions inside that boundary before storing any company-owned data.
    from django.contrib.auth import get_user_model
    get_user_model().objects.select_for_update().get(pk=principal.user.pk)
    principal = valid_grant(principal.grant["id"], scope="startup:draft:write")
    company = company_for(principal.user, arguments["companyId"], principal.grant)
    _brief(company, month)
    update_id = arguments.get("updateId")
    payload_hash = content_hash(arguments)
    if update_id:
        draft = MonthlyUpdateDraft.objects.select_for_update().filter(pk=update_id, organization=company.organization).first()
        if draft is None:
            raise ValidationError("This update is unavailable to this startup.")
        if draft.month != month:
            raise ValidationError("This update belongs to another reporting month.")
    else:
        zone = getattr(getattr(company.organization, "startup_profile", None), "reporting_timezone", "UTC") or "UTC"
        today = timezone.now().astimezone(ZoneInfo(zone)).date()
        next_month = date(month.year + 1, 1, 1) if month.month == 12 else date(month.year, month.month + 1, 1)
        draft, _ = resolve_update(company.organization, month=month, creation_key=request_id, update_date=min(today, next_month - timedelta(days=1)))
    if draft.month != month:
        raise ValidationError("This draft identity belongs to another reporting month. Use a new requestId.")
    current = draft.current_revision if draft.current_revision_id else None
    old = copy.deepcopy(current.structured_memo if current else {})
    receipt = (old.get("_agent_requests") or {}).get(request_id)
    if not receipt and draft.current_revision_id:
        historical = draft.revisions.filter(structured_memo___agent_requests__has_key=request_id).order_by("-number").first()
        receipt = ((historical.structured_memo.get("_agent_requests") or {}).get(request_id)) if historical else None
    if receipt:
        if receipt["hash"] != payload_hash or receipt["user_id"] != principal.user.pk:
            raise RevisionConflict("This requestId was already used for different content.")
        return {**status_payload(company, draft), "replayed": True}
    if draft.current_revision_id and arguments.get("expectedRevision") != draft.current_revision_id:
        raise RevisionConflict()
    agent_validation = {"groundedness_status": "needs_review", "provenance": "agent_supplied",
        "source_verification": "unverified_external_agent"}
    validation = agent_validation
    if current:
        prior_validation = current.validation or {}
        prior_agent_review = (prior_validation.get("groundedness_status") == "needs_review"
            and prior_validation.get("provenance") == "agent_supplied"
            and prior_validation.get("source_verification") == "unverified_external_agent"
            and (old.get("_agent_provenance") or {}).get("kind") == "agent_supplied")
        # Imported text must never turn unresolved carried claims into the
        # narrower agent-only review classification. Passed revisions need a
        # fresh agent review for the new text; all other review state survives.
        if prior_validation.get("groundedness_status") not in {"passed", "founder_asserted"} and not prior_agent_review:
            validation = copy.deepcopy(prior_validation)
    memo = old
    for incoming, field in NARRATIVE_FIELDS.items():
        if incoming in arguments["narrative"]:
            value = arguments["narrative"][incoming].strip()
            memo[field] = value if field == "summary" else [line.strip() for line in value.splitlines() if line.strip()]
    memo["_agent_sources"] = copy.deepcopy(arguments.get("sources", []))
    memo["_agent_provenance"] = {"kind": "agent_supplied", "client": principal.grant["clientName"],
        "submitted_at": timezone.now().isoformat(), "coverage_notes": arguments.get("coverageNotes", "")}
    receipts = memo.setdefault("_agent_requests", {})
    if len(receipts) >= 100:
        raise ValidationError("This draft has reached its import limit. Create another draft.")
    receipts[request_id] = {"hash": payload_hash, "user_id": principal.user.pk}
    snapshot = draft.current_revision.snapshot if draft.current_revision_id else capture_snapshot(company.organization, month)
    save_revision(draft, memo, snapshot=snapshot, audience="private", expected_revision=arguments.get("expectedRevision"),
        validation=validation)
    draft.refresh_from_db()
    draft.title = draft.title or f"{company.name} {month.strftime('%B %Y')} Update"
    draft.run = None
    draft.save(update_fields=["title", "run", "updated_at"])
    return {**status_payload(company, draft), "replayed": False}


def call_tool(principal, name, arguments):
    definition = next((item for item in TOOLS if item["name"] == name), None)
    if not definition:
        raise ValidationError("Unknown Valley tool.")
    schema = definition["inputSchema"]
    if not isinstance(arguments, dict) or set(arguments) - schema["properties"].keys() or set(schema["required"]) - arguments.keys():
        raise ValidationError("Provide the required supported tool arguments only.")
    scope = "startup:draft:write" if name == "save_narrative_draft" else "startup:brief:read"
    principal = valid_grant(principal.grant["id"], scope=scope)
    if name == "list_startups":
        company = company_for(principal.user, principal.grant["company_id"], principal.grant)
        return {"startups": [{"id": str(company.pk), "name": company.name}]}
    company = company_for(principal.user, arguments["companyId"], principal.grant)
    if name == "get_monthly_update_brief":
        return _brief(company, month_date(arguments["month"]))
    if name == "save_narrative_draft":
        return save_draft(principal, arguments)
    update_id = arguments.get("updateId")
    if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 1:
        raise ValidationError("Use a positive updateId.")
    draft = MonthlyUpdateDraft.objects.select_related("current_revision").filter(pk=update_id, organization=company.organization).first()
    if draft is None:
        raise ValidationError("This update is unavailable to this startup.")
    return status_payload(company, draft)
