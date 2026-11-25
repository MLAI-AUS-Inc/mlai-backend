"""Separate authority for public meeting discovery and private treasury decisions."""
import hmac
import re

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRooApiKey
from .services.studio_payment_rules import ReviewError


class HasStudioTreasuryKey(BasePermission):
    def has_permission(self, request, view):
        key = getattr(settings, "STUDIO_TREASURY_KEY", "")
        public_keys = {getattr(settings, name, "") for name in ("ROO_API_KEY", "INTERNAL_API_KEY", "STUDIO_REPORT_SOURCE_KEY")}
        return (getattr(settings, "STUDIO_TREASURY_ENABLED", False) and len(key) >= 32 and key not in public_keys
                and hmac.compare_digest(key.encode(), request.headers.get("X-Studio-Treasury-Key", "").encode()))


def identity(data):
    workspace, actor = data.get("workspace", ""), data.get("actor", "")
    if not re.fullmatch(r"T[A-Z0-9]+", workspace) or not re.fullmatch(r"[UW][A-Z0-9]+", actor):
        raise ReviewError("invalid_slack_identity")
    return workspace, actor


def failure(exc):
    if isinstance(exc, PermissionError):
        return Response({"error": "studio_request_not_authorized"}, status=403)
    if isinstance(exc, ObjectDoesNotExist):
        return Response({"error": "studio_setup_or_review_missing"}, status=404)
    if isinstance(exc, ReviewError):
        return Response({"error": str(exc)}, status=409)
    # Provider response bodies and configuration paths stay out of Slack and logs.
    return Response({"error": "studio_source_unavailable"}, status=503)


class StudioTreasuryView(APIView):
    authentication_classes = []
    permission_classes = [HasStudioTreasuryKey]
    throttle_classes = []

    def post(self, request):
        from .services.studio_treasury import perform
        from .services.studio_meetings import confirm, pending_times, pending_links, link, enrich
        from .services.studio_treasury_sources import StudioSources
        try:
            workspace, actor = identity(request.data)
            operation = request.data["operation"]
            if operation == "meeting_scan":
                from datetime import datetime, timedelta
                from zoneinfo import ZoneInfo
                from .services.studio_treasury import scope_for
                scope = scope_for(workspace, actor, treasury=False)
                start = datetime.fromisoformat(request.data["start"]).replace(tzinfo=ZoneInfo("Australia/Melbourne"))
                end = datetime.fromisoformat(request.data["end"]).replace(tzinfo=ZoneInfo("Australia/Melbourne")) + timedelta(days=1)
                mode = request.data.get("mode", "review")
                scanned = enrich(workspace, actor, list(scope.configuration["projects"]), mode=mode,
                                 start=start.isoformat(), end=end.isoformat())
                return Response({"meetings": pending_times(workspace, actor), "links": pending_links(workspace, actor),
                    "calendar_scan": scanned, "scan": {"start": request.data["start"], "end": request.data["end"], "mode": mode}})
            if operation == "meeting_review":
                return Response({"meetings": pending_times(workspace, actor), "links": pending_links(workspace, actor)})
            if operation == "meeting_link":
                return Response({"linked": link(workspace, actor, request.data["meeting"], request.data["issue"])})
            if operation == "meeting_time":
                result = confirm(workspace, actor, request.data["meeting"], request.data["minutes"],
                                 builder=request.data.get("builder"), approved=request.data.get("approved", False))
                from .services.studio_treasury import scope_for
                scope = scope_for(workspace, actor, treasury=False)
                return Response({"attendance": result,
                    "notify_reviewers": scope.configuration["treasurers"] if not result["approved"] else []})
            key = request.data.get("request_key", "")
            if not re.fullmatch(r"[a-f0-9]{64}", key):
                raise ReviewError("request_identity_missing")
            result = perform(workspace, actor, operation, request_key=key,
                selector=request.data.get("selector", "recent"), review_id=request.data.get("review_id"),
                invoice=request.data.get("invoice"), receipt_id=request.data.get("receipt_id"),
                source_factory=lambda scope: StudioSources(scope, request.data.get("sealed_artifacts")))
            return Response(result)
        except Exception as exc:
            return failure(exc)


class StudioMeetingCalendarView(APIView):
    permission_classes = [HasRooApiKey]

    def post(self, request):
        from .services.studio_meetings import enrich
        if not getattr(settings, "STUDIO_MEETINGS_ENABLED", False):
            return Response({"meetings": []})
        try:
            workspace, actor = identity(request.data)
            projects = request.data.get("projects", [])
            if not isinstance(projects, list) or not 1 <= len(projects) <= 5:
                raise ReviewError("invalid_project_scope")
            return Response({"meetings": enrich(workspace, actor, projects,
                mode=request.data.get("mode", "review"), start=request.data.get("start"), end=request.data.get("end"))})
        except Exception as exc:
            return failure(exc)
