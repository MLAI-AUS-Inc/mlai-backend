"""Independent founder publications; month-keyed callers have a separate legacy slot."""
from datetime import date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import NotFound, ValidationError

from organizations.models import Organization
from startup_updates.models import MonthlyUpdateDraft
from startup_updates.revisions import RevisionConflict


@transaction.atomic
def resolve_update(organization, *, month, update_id=None, creation_key=None, update_date=None):
    Organization.objects.select_for_update().get(pk=organization.pk)
    drafts = MonthlyUpdateDraft.objects.filter(organization=organization)
    if update_id:
        draft = drafts.filter(pk=update_id).first()
        if draft is None:
            raise NotFound("This update does not belong to the selected startup.")
        if creation_key and draft.creation_key and str(draft.creation_key) != str(creation_key):
            raise RevisionConflict("The draft identity changed. Reopen this update.")
        return draft, False
    if creation_key:
        try:
            key = UUID(str(creation_key))
        except (ValueError, TypeError, AttributeError):
            raise ValidationError({"creationKey": "Use a valid draft creation key."})
        month = update_date.replace(day=1) if update_date else month
        return drafts.get_or_create(organization=organization, creation_key=key, defaults={"month": month, "update_date": update_date})
    # An older client must never overwrite an arbitrary independent publication.
    if drafts.filter(month=month, creation_key__isnull=False).exists():
        raise RevisionConflict("Choose an update to edit, or reopen New update to create a separate draft.")
    return drafts.monthly_slots().get_or_create(organization=organization, month=month)


def run_update(run, month, *, create=False):
    # ContentFactoryRun keeps organization in its request, not necessarily a FK.
    organization_id = (run.run_request or {}).get("organization_id")
    query = MonthlyUpdateDraft.objects.filter(organization_id=organization_id)
    update_id = (run.run_request or {}).get("update_id")
    if update_id:
        draft = query.filter(pk=update_id).first()
        if draft is None or draft.month != month:
            raise RevisionConflict("This run targets a different update or reporting period.")
        return draft
    return query.monthly_slots().get_or_create(organization_id=organization_id, month=month)[0] if create else query.monthly_slots().filter(month=month).first()


def parse_generation_date(data, *, update_id):
    """Require a valid explicit date; only an existing update may omit it."""
    if update_id and "updateDate" not in data:
        return None
    try:
        return date.fromisoformat(str(data.get("updateDate")))
    except (ValueError, TypeError) as exc:
        raise ValidationError({"updateDate": "Choose a valid update date."}) from exc


def default_generation_date(draft, *, today):
    """Choose a date for an existing update when an older client omits one."""
    if draft.update_date:
        return draft.update_date
    month = draft.month
    if today < month:
        raise ValidationError({"updateDate": "Choose today or an earlier reporting month."})
    next_month = date(month.year + 1, 1, 1) if month.month == 12 else date(month.year, month.month + 1, 1)
    return min(next_month - timedelta(days=1), today)


def previous_publications(organization, update_id, update_date):
    """Chronology comes from the published revision, even while its date is edited."""
    publications = []
    for item in MonthlyUpdateDraft.objects.filter(organization=organization, published_at__isnull=False).exclude(pk=update_id).select_related("published_revision"):
        memo = item.published_revision.structured_memo if item.published_revision_id else item.structured_memo
        value = memo.get("update_date") or (None if item.published_revision_id else (item.update_date.isoformat() if item.update_date else None))
        key = value or item.month.isoformat()[:7]
        if key <= update_date.isoformat():
            publications.append((item, memo, key))
    return sorted(publications, key=lambda entry: (entry[2], entry[0].first_published_at or entry[0].published_at, entry[0].pk), reverse=True)


def narrative_window(organization, draft, update_date, *, requested_start=None, requested_end=None):
    zone_name = getattr(getattr(organization, "startup_profile", None), "reporting_timezone", "UTC")
    zone = ZoneInfo(zone_name)
    now = timezone.now()
    if update_date > now.astimezone(zone).date():
        raise ValidationError({"updateDate": "Choose today or an earlier date."})
    end = min(datetime.combine(update_date + timedelta(days=1), time.min, zone), now)
    start = datetime.combine(update_date.replace(day=1), time.min, zone)
    for item, memo, prior_date in previous_publications(organization, draft.pk, update_date):
        cutoff = (memo.get("narrative_period") or {}).get("end")
        if not cutoff or len(prior_date) != 10:
            continue
        candidate = parse_datetime(str(cutoff))
        if candidate and timezone.is_aware(candidate) and candidate < end:
            start = candidate
            break
    for value, field in ((requested_start, "start"), (requested_end, "end")):
        if not value:
            continue
        parsed = parse_datetime(str(value))
        if parsed is None or timezone.is_naive(parsed):
            raise ValidationError({"narrativePeriod": "Use timestamps with an explicit timezone."})
        if field == "start":
            start = parsed
        else:
            end = parsed
    limit = min(datetime.combine(update_date + timedelta(days=1), time.min, zone), now)
    if start >= end or end > limit:
        raise ValidationError({"narrativePeriod": "Choose a source range ending on or before the update date and current time."})
    return {"start": start.isoformat(), "end": end.isoformat(), "timezone": zone_name, "end_exclusive": True}


def identity_payload(draft, memo=None):
    provided_memo = memo is not None
    memo = memo if provided_memo else {}
    # Published revisions retain their reviewed date while a new date is being edited.
    value = memo.get("update_date", draft.update_date.isoformat() if draft.update_date else None)
    if provided_memo and draft.published_at and not draft.published_revision_id and "update_date" not in memo:
        value = None  # A legacy publication has month precision until a dated revision is approved.
    return {"updateId": draft.pk, "creationKey": str(draft.creation_key) if draft.creation_key else None,
            "updateDate": value, "datePrecision": "day" if value else "month",
            "firstPublishedAt": draft.first_published_at.isoformat() if draft.first_published_at else None,
            "narrativePeriod": memo.get("narrative_period")}
