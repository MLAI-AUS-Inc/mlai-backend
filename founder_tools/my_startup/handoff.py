"""Short-lived, account-bound recovery of old-origin research drafts."""

import hashlib
import json
import secrets
from uuid import UUID
from urllib.parse import urlencode

from django.core.cache import cache
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from founder_tools.models import VibeRaisingCompany
from workflow_runs.models import ContentFactoryRun
from .api import MyStartupViewMixin
from .links import frontend_origin, page_path

TTL = 600


def company_for_handoff(user, company_id):
    try:
        company_id = UUID(str(company_id))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Invalid startup identity.")
    return get_object_or_404(
        VibeRaisingCompany.objects.select_related("organization"),
        pk=company_id,
        profile__user=user,
    )


def validate_bundle(raw, company):
    if not isinstance(raw, dict) or len(json.dumps(raw).encode()) > 16_384:
        raise ValidationError("Research draft is invalid or too large.")
    brief = raw.get("brief", {})
    if not isinstance(brief, dict):
        raise ValidationError("Invalid research brief.")
    selected = raw.get("selected", [])
    if (
        not isinstance(selected, list)
        or len(selected) > 100
        or any(not isinstance(item, str) or len(item) > 200 for item in selected)
    ):
        raise ValidationError("Invalid island selection.")
    run_id = raw.get("runId")
    if run_id:
        if (
            not isinstance(run_id, str)
            or len(run_id) > 100
            or not company.organization_id
        ):
            raise ValidationError("Invalid research run.")
        get_object_or_404(
            ContentFactoryRun,
            run_id=run_id,
            organization_id=company.organization_id,
            workflow="island_refresh",
            run_request__island_research_brief__isnull=False,
        )
    request_id = raw.get("requestId")
    if request_id is not None and (
        not isinstance(request_id, str) or len(request_id) > 200
    ):
        raise ValidationError("Invalid research request identity.")
    step = raw.get("step", 0)
    if not isinstance(step, int) or step not in {0, 1, 2}:
        raise ValidationError("Invalid research step.")
    return {
        "brief": brief,
        "step": step,
        "runId": run_id,
        "selected": selected,
        "requestId": request_id,
    }


def cache_key(token):
    return "my-startup-handoff:" + hashlib.sha256(token.encode()).hexdigest()


class CreateStartupHandoffView(APIView):
    """Legacy-JWT entrypoint; does not change or combine account identities."""

    permission_classes = (IsAuthenticated,)

    def post(self, request):
        company = company_for_handoff(request.user, request.data.get("companyId"))
        bundle = validate_bundle(request.data.get("research", {}), company)
        old = str(request.data.get("path") or "/founder-tools/marketing")
        # Only the enumerated frontend marketing routes migrate.
        from urllib.parse import urlsplit, parse_qsl, urlunsplit

        parsed = urlsplit(old)
        destination = page_path(parsed.path)
        if parsed.scheme or parsed.netloc or not destination or "\\" in old:
            raise ValidationError("Invalid migration destination.")
        query = [
            (key, value)
            for key, value in parse_qsl(parsed.query)
            if key
            in {
                "step",
                "articleStep",
                "setupStep",
                "reviewMode",
                "runId",
                "scanRunId",
                "setupRunId",
                "researchRunId",
                "topic",
                "keyword",
                "q",
                "audience_id",
                "offer_id",
                "offset",
            }
            and len(value) <= 500
        ]
        query.append(("company_id", str(company.pk)))
        path = urlunsplit(("", "", destination, urlencode(query), ""))
        token = secrets.token_urlsafe(32)
        cache.set(
            cache_key(token),
            {
                "userId": request.user.pk,
                "companyId": str(company.pk),
                "research": bundle,
                "path": path,
            },
            TTL,
        )
        return Response(
            {
                "url": f"{frontend_origin()}/my-startup/handoff?token={token}",
                "expiresIn": TTL,
            },
            status=201,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )


class RedeemStartupHandoffView(MyStartupViewMixin, APIView):
    def post(self, request):
        token = request.data.get("token")
        if not isinstance(token, str) or len(token) > 100:
            raise ValidationError("Invalid migration link.")
        key = cache_key(token)
        value = cache.get(key)
        if not value or value["userId"] != request.user.pk:
            return Response(
                {
                    "detail": "This migration link is expired or belongs to a different account."
                },
                status=404,
            )
        company = company_for_handoff(request.user, value["companyId"])
        validate_bundle(value["research"], company)
        if not cache.add(key + ":used", True, TTL):
            return Response(
                {"detail": "This migration link has already been used."}, status=409
            )
        cache.delete(key)
        # Retrieval never dispatches work, adopts islands, or spends Roo points.
        return Response(
            {
                "companyId": value["companyId"],
                "research": value["research"],
                "path": value["path"],
            }
        )
