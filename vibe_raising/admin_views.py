"""Admin (organiser) dashboard endpoints for Vibe Raising.

These power the standalone admin dashboard at admin.mlai.au: an overview with
headline stats + charts, monthly-update adoption metrics, a filterable list of
every startup's monthly updates, and a per-update detail view. Access is gated to MLAI organisers via
``roo.permissions.is_points_admin_user`` (Django superusers always count).

Data source is ``startup_updates.MonthlyUpdateDraft`` (one row per org per
month); founder/company identity is resolved through
``founder_tools.VibeRaisingCompany``. Response keys are camelCase to match the
existing vibe_raising API and the frontend normalizers in
``app/lib/vibe-raising.ts``.
"""
import calendar
import logging
from datetime import date

from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from roo.permissions import is_points_admin_user
from startup_updates.admin_metrics import build_monthly_update_usage_payload
from startup_updates.models import MonthlyUpdateDraft, MonthlyUpdateDraftStatus

from .views import _month_start, _previous_month_start, _serialize_monthly_update

logger = logging.getLogger(__name__)

RECENT_UPDATES_LIMIT = 8
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
TIMELINE_MONTHS = 6

IN_PROGRESS_STATUSES = (
    MonthlyUpdateDraftStatus.DRAFT,
    MonthlyUpdateDraftStatus.NEEDS_REVIEW,
)

# Map the frontend's looser status-filter vocabulary onto draft statuses.
_STATUS_FILTER_ALIASES = {
    "draft": MonthlyUpdateDraftStatus.DRAFT,
    "needs_review": MonthlyUpdateDraftStatus.NEEDS_REVIEW,
    "review": MonthlyUpdateDraftStatus.NEEDS_REVIEW,
    "in_review": MonthlyUpdateDraftStatus.NEEDS_REVIEW,
    "ready": MonthlyUpdateDraftStatus.READY,
    "published": MonthlyUpdateDraftStatus.READY,
    "error": MonthlyUpdateDraftStatus.ERROR,
}


class IsVibeRaisingAdmin(permissions.BasePermission):
    """Allow only MLAI organisers (PointsAdmin) / superusers."""

    message = "Vibe Raising admin access is required."

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated and is_points_admin_user(user))


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _month_label(value: date) -> str:
    return f"{calendar.month_name[value.month]} {value.year}"


def _humanize(label: str) -> str:
    return label.replace("_", " ").replace("-", " ").strip().title()


def _admin_drafts_queryset():
    """Drafts with the relations needed to resolve startup + founder, cheaply."""
    return MonthlyUpdateDraft.objects.select_related("organization").prefetch_related(
        "organization__founder_companies__profile__user"
    )


def _resolve_company_and_founder(organization):
    """Pick the best VibeRaisingCompany for an org, and its founder user."""
    companies = list(organization.founder_companies.all()) if organization else []
    company = next((c for c in companies if c.registered), None)
    if company is None and companies:
        company = companies[0]
    founder = None
    if company is not None:
        profile = getattr(company, "profile", None)
        founder = getattr(profile, "user", None)
    return company, founder


def _summary_for_draft(draft, company=None, founder=None) -> dict:
    if company is None and founder is None:
        company, founder = _resolve_company_and_founder(draft.organization)
    startup_name = (
        (company.name if company else None)
        or getattr(draft.organization, "domain", None)
        or "Unknown startup"
    )
    return {
        # Must be a string: the frontend normalizer (normalizeAdminUpdateSummary)
        # drops any summary whose id is not a string, which silently empties
        # recentUpdates and the updates list.
        "id": str(draft.id),
        "startupName": startup_name,
        "startupAvatarUrl": company.avatar_url if company else None,
        "updateMonth": _month_label(draft.month),
        "status": draft.status,
        "lastUpdatedAt": draft.updated_at.isoformat(),
        "founderName": (founder.full_name if founder else None) or None,
        "companyId": str(company.id) if company else None,
    }


def _trend(current: int, previous: int):
    """Return (trendLabel, trendDirection) comparing this month to last month."""
    if previous <= 0:
        if current > 0:
            return f"{current} new vs last month", "up"
        return None, "neutral"
    delta = round((current - previous) / previous * 100)
    if delta > 0:
        return f"{delta}% vs last month", "up"
    if delta < 0:
        return f"{abs(delta)}% vs last month", "down"
    return "No change vs last month", "neutral"


def _breakdown_from_rows(rows, key):
    pairs = []
    total = 0
    for row in rows:
        label = (row.get(key) or "").strip()
        count = row.get("count") or 0
        if not label or count <= 0:
            continue
        pairs.append((label, count))
        total += count
    pairs.sort(key=lambda pair: pair[1], reverse=True)
    breakdown = []
    for label, count in pairs:
        pct = round(count / total * 100) if total else 0
        breakdown.append(
            {
                "label": _humanize(label),
                "value": count,
                "percentage": pct,
                "percentageText": f"{pct}%",
            }
        )
    return breakdown


class VibeRaisingAdminOverviewView(APIView):
    permission_classes = [IsVibeRaisingAdmin]

    def get(self, request):
        today = timezone.now().date()
        current_month = _month_start(today)
        previous_month = _previous_month_start(current_month)

        def month_counts(month):
            base = MonthlyUpdateDraft.objects.filter(month=month)
            return {
                "startups": base.values("organization").distinct().count(),
                "updates": base.count(),
                "ready": base.filter(status=MonthlyUpdateDraftStatus.READY).count(),
                "in_progress": base.filter(status__in=IN_PROGRESS_STATUSES).count(),
            }

        cur = month_counts(current_month)
        prev = month_counts(previous_month)

        def stat(key, label, cur_value, prev_value):
            trend_label, trend_direction = _trend(cur_value, prev_value)
            return {
                "key": key,
                "label": label,
                "value": cur_value,
                "trendLabel": trend_label,
                "trendDirection": trend_direction,
            }

        stats = [
            stat("startupsCreatingUpdates", "Startups creating updates", cur["startups"], prev["startups"]),
            stat("updatesCreated", "Updates created", cur["updates"], prev["updates"]),
            stat("publishedUpdates", "Updates ready", cur["ready"], prev["ready"]),
            stat("draftsInProgress", "Drafts in progress", cur["in_progress"], prev["in_progress"]),
        ]

        # Updates over time: last TIMELINE_MONTHS buckets by target month.
        timeline_start = current_month
        for _ in range(TIMELINE_MONTHS - 1):
            timeline_start = _previous_month_start(timeline_start)
        counts_by_month = {
            row["month"]: row["count"]
            for row in MonthlyUpdateDraft.objects.filter(month__gte=timeline_start)
            .values("month")
            .annotate(count=Count("id"))
        }
        updates_over_time = []
        cursor = timeline_start
        while cursor <= current_month:
            updates_over_time.append(
                {"label": _month_label(cursor), "value": counts_by_month.get(cursor, 0)}
            )
            cursor = _next_month(cursor)

        # Updates by funding stage (from each org's StartupProfile.stage).
        updates_by_stage = _breakdown_from_rows(
            MonthlyUpdateDraft.objects.values("organization__startup_profile__stage").annotate(
                count=Count("id")
            ),
            "organization__startup_profile__stage",
        )
        recent_updates = [
            _summary_for_draft(draft)
            for draft in _admin_drafts_queryset().order_by("-updated_at")[:RECENT_UPDATES_LIMIT]
        ]

        review_count = MonthlyUpdateDraft.objects.filter(
            status=MonthlyUpdateDraftStatus.NEEDS_REVIEW
        ).count()

        return Response(
            {
                "stats": stats,
                "updatesOverTime": updates_over_time,
                "updatesByStage": updates_by_stage,
                # No first-class industry field on the startup profile yet.
                "updatesByIndustry": [],
                "recentUpdates": recent_updates,
                "reviewCount": review_count,
            },
            status=status.HTTP_200_OK,
        )


class VibeRaisingAdminMonthlyUpdateUsageView(APIView):
    """Current connector adoption and completed AI-assisted update usage."""

    permission_classes = [IsVibeRaisingAdmin]

    def get(self, request):
        return Response(build_monthly_update_usage_payload(), status=status.HTTP_200_OK)


class VibeRaisingAdminUpdatesView(APIView):
    permission_classes = [IsVibeRaisingAdmin]

    def get(self, request):
        queryset = _admin_drafts_queryset().order_by("-updated_at")

        status_param = (request.query_params.get("status") or "").strip().lower()
        if status_param:
            queryset = queryset.filter(status=_STATUS_FILTER_ALIASES.get(status_param, status_param))

        search = (request.query_params.get("q") or "").strip()
        if search:
            queryset = queryset.filter(
                Q(organization__founder_companies__name__icontains=search)
                | Q(organization__domain__icontains=search)
                | Q(organization__founder_companies__profile__user__first_name__icontains=search)
                | Q(organization__founder_companies__profile__user__last_name__icontains=search)
                | Q(organization__founder_companies__profile__user__email__icontains=search)
            ).distinct()

        try:
            page_number = int(request.query_params.get("page") or 1)
        except (TypeError, ValueError):
            page_number = 1
        try:
            page_size = int(
                request.query_params.get("pageSize")
                or request.query_params.get("page_size")
                or DEFAULT_PAGE_SIZE
            )
        except (TypeError, ValueError):
            page_size = DEFAULT_PAGE_SIZE
        page_size = max(1, min(page_size, MAX_PAGE_SIZE))

        paginator = Paginator(queryset, page_size)
        page = paginator.get_page(page_number)

        return Response(
            {
                "updates": [_summary_for_draft(draft) for draft in page.object_list],
                "total": paginator.count,
                "page": page.number,
                "pageSize": page_size,
                "hasNext": page.has_next(),
                "hasPrevious": page.has_previous(),
            },
            status=status.HTTP_200_OK,
        )


class VibeRaisingAdminUpdateDetailView(APIView):
    permission_classes = [IsVibeRaisingAdmin]

    def get(self, request, update_id):
        draft = _admin_drafts_queryset().filter(pk=update_id).first()
        if draft is None:
            return Response({"detail": "Update not found."}, status=status.HTTP_404_NOT_FOUND)

        company, founder = _resolve_company_and_founder(draft.organization)
        summary = _summary_for_draft(draft, company=company, founder=founder)

        company_payload = None
        if company is not None:
            company_payload = {
                "id": str(company.id),
                "name": company.name,
                "domain": company.domain,
                "avatarUrl": company.avatar_url,
                "location": company.location,
                "registered": company.registered,
            }

        founder_payload = None
        if founder is not None:
            founder_payload = {"name": founder.full_name or None, "email": founder.email}

        return Response(
            {
                "summary": summary,
                "update": _serialize_monthly_update(draft),
                "company": company_payload,
                "founder": founder_payload,
            },
            status=status.HTTP_200_OK,
        )
