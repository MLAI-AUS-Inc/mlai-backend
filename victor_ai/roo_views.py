"""Channel-bound, read-only Victor application views for Roo."""

from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from io import StringIO
from typing import Optional

from django.conf import settings
from django.db.models import Count, Q
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import VictorApplication, VictorApplicationAccessAudit
from .roo_auth import HasVictorRooAccess
from .serializers import (
    VictorApplicationDetailSerializer,
    VictorApplicationListSerializer,
)


FILTER_KEYS = (
    "stage",
    "role",
    "startup_stage",
    "industry_sector",
    "created_after",
    "created_before",
    "q",
)
CSV_FIELDS = (
    "id",
    "stage",
    "first_name",
    "last_name",
    "email",
    "linkedin",
    "team_name",
    "role",
    "startup_stage",
    "industry_sector",
    "location",
    "team_size",
    "team_members",
    "revenue_last_3_months",
    "idea",
    "support",
    "consent",
    "created_at",
    "updated_at",
)


def _parse_date(value: str, field: str) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError({field: "Use an ISO date in YYYY-MM-DD format."}) from exc


def _query_filters(request) -> dict:
    values = {
        key: str(request.query_params.get(key, "") or "").strip()
        for key in FILTER_KEYS
    }
    if values["stage"] and values["stage"] not in {
        VictorApplication.STAGE_LEAD,
        VictorApplication.STAGE_COMPLETE,
    }:
        raise ValidationError({"stage": "Use `lead` or `complete`."})
    if len(values["q"]) > 200:
        raise ValidationError({"q": "Search text must be 200 characters or fewer."})
    _parse_date(values["created_after"], "created_after")
    _parse_date(values["created_before"], "created_before")
    return {key: value for key, value in values.items() if value}


def _filtered_applications(filters: dict):
    queryset = VictorApplication.objects.all()
    if filters.get("stage"):
        queryset = queryset.filter(stage=filters["stage"])
    for field in ("role", "startup_stage", "industry_sector"):
        if filters.get(field):
            queryset = queryset.filter(**{f"{field}__iexact": filters[field]})
    if filters.get("created_after"):
        queryset = queryset.filter(
            created_at__date__gte=_parse_date(filters["created_after"], "created_after")
        )
    if filters.get("created_before"):
        queryset = queryset.filter(
            created_at__date__lte=_parse_date(filters["created_before"], "created_before")
        )
    if filters.get("q"):
        value = filters["q"]
        queryset = queryset.filter(
            Q(first_name__icontains=value)
            | Q(last_name__icontains=value)
            | Q(email__icontains=value)
            | Q(team_name__icontains=value)
        )
    return queryset.order_by("-created_at", "-id")


def _coerce_int(request, name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError({name: "Use a whole number."}) from exc
    if value < minimum or value > maximum:
        raise ValidationError({name: f"Use a value from {minimum} to {maximum}."})
    return value


def _audit_filters(filters: dict) -> dict:
    safe = {key: value for key, value in filters.items() if key != "q"}
    if filters.get("q"):
        safe["has_search"] = True
    return safe


def _record_access(
    request,
    *,
    action: str,
    filters: dict,
    row_count: int,
    target_application_id: Optional[int] = None,
    outcome: str = "success",
) -> None:
    actor = request.victor_roo_actor
    VictorApplicationAccessAudit.objects.create(
        action=action,
        slack_team_id=actor.slack_team_id,
        slack_channel_id=actor.slack_channel_id,
        acting_slack_user_id=actor.slack_user_id,
        request_id=actor.request_id,
        target_application_id=target_application_id,
        filters=_audit_filters(filters),
        row_count=max(0, int(row_count)),
        outcome=outcome,
    )


def _breakdown(queryset, field: str, limit: int = 8) -> list[dict]:
    rows = (
        queryset.exclude(**{field: ""})
        .values(field)
        .annotate(count=Count("id"))
        .order_by("-count", field)[:limit]
    )
    return [{"value": row[field], "count": row["count"]} for row in rows]


class VictorRooView(APIView):
    authentication_classes = []
    permission_classes = [HasVictorRooAccess]


class VictorApplicationSummaryView(VictorRooView):
    def get(self, request):
        filters = _query_filters(request)
        queryset = _filtered_applications(filters)
        complete = queryset.filter(stage=VictorApplication.STAGE_COMPLETE)
        today = timezone.localdate()
        payload = {
            "total_records": queryset.count(),
            "complete_count": complete.count(),
            "lead_count": queryset.filter(stage=VictorApplication.STAGE_LEAD).count(),
            "complete_created_today": complete.filter(created_at__date=today).count(),
            "complete_created_last_7_days": complete.filter(
                created_at__date__gte=today - timedelta(days=6)
            ).count(),
            "breakdowns": {
                "startup_stage": _breakdown(complete, "startup_stage"),
                "industry_sector": _breakdown(complete, "industry_sector"),
            },
            "filters": filters,
        }
        _record_access(
            request,
            action="summary",
            filters=filters,
            row_count=payload["total_records"],
        )
        return Response(payload)


class VictorApplicationListView(VictorRooView):
    def get(self, request):
        filters = _query_filters(request)
        limit = _coerce_int(request, "limit", default=10, minimum=1, maximum=20)
        offset = _coerce_int(request, "offset", default=0, minimum=0, maximum=100000)
        queryset = _filtered_applications(filters)
        total_count = queryset.count()
        rows = list(queryset[offset : offset + limit])
        payload = {
            "applications": VictorApplicationListSerializer(rows, many=True).data,
            "total_count": total_count,
            "returned_count": len(rows),
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total_count,
            "filters": filters,
        }
        _record_access(
            request,
            action="list",
            filters=filters,
            row_count=len(rows),
        )
        return Response(payload)


class VictorApplicationDetailView(VictorRooView):
    def get(self, request, application_id: int):
        application = VictorApplication.objects.filter(pk=application_id).first()
        if application is None:
            _record_access(
                request,
                action="detail",
                filters={},
                row_count=0,
                target_application_id=application_id,
                outcome="not_found",
            )
            return Response({"detail": "Application not found."}, status=status.HTTP_404_NOT_FOUND)
        _record_access(
            request,
            action="detail",
            filters={},
            row_count=1,
            target_application_id=application_id,
        )
        return Response(VictorApplicationDetailSerializer(application).data)


def _csv_safe(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, (list, dict)):
        rendered = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    elif hasattr(value, "isoformat"):
        rendered = value.isoformat()
    else:
        rendered = str(value)
    if rendered and rendered[0] in {"=", "+", "-", "@", "\t", "\r"}:
        return "'" + rendered
    return rendered


class VictorApplicationCsvView(VictorRooView):
    def get(self, request):
        filters = _query_filters(request)
        queryset = _filtered_applications(filters)
        max_rows = int(getattr(settings, "VICTOR_AI_ROO_EXPORT_MAX_ROWS", 5000))
        row_count = queryset.count()
        if row_count > max_rows:
            _record_access(
                request,
                action="export_csv",
                filters=filters,
                row_count=0,
                outcome="too_large",
            )
            return Response(
                {
                    "detail": (
                        f"The export matches {row_count} applications; narrow the filters "
                        f"to {max_rows} rows or fewer."
                    )
                },
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )

        output = StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(CSV_FIELDS)
        for application in queryset.iterator(chunk_size=500):
            writer.writerow([_csv_safe(getattr(application, field)) for field in CSV_FIELDS])

        filename = f"victor-ai-applications-{timezone.localdate().isoformat()}.csv"
        response = HttpResponse(output.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["X-Content-Type-Options"] = "nosniff"
        _record_access(
            request,
            action="export_csv",
            filters=filters,
            row_count=row_count,
        )
        return response
