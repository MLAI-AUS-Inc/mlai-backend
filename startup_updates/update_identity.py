"""One working update per startup/month, retaining older dated records as history."""
from datetime import date, timedelta
from uuid import UUID

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import NotFound, ValidationError

from organizations.models import Organization
from startup_updates.models import MonthlyUpdateDraft
from startup_updates.revisions import RevisionConflict
from startup_updates.monthly_groups import latest_monthly_draft


@transaction.atomic
def resolve_update(organization, *, month, update_id=None, creation_key=None, update_date=None):
    """Serialize month creation and resume its existing copy across all clients."""
    Organization.objects.select_for_update().get(pk=organization.pk)
    drafts = MonthlyUpdateDraft.objects.filter(organization=organization)
    month = month.replace(day=1)
    requested = None
    if update_id:
        requested = drafts.filter(pk=update_id).first()
        if requested is None:
            raise NotFound("This update does not belong to the selected startup.")
        # An old link still belongs to its original reporting month.
        month = requested.month.replace(day=1)
    if creation_key:
        try:
            UUID(str(creation_key))
        except (ValueError, TypeError, AttributeError):
            raise ValidationError({"creationKey": "Use a valid draft creation key."})
    draft = latest_monthly_draft(drafts, month)
    if draft is not None:
        if requested is not None and draft.pk != requested.pk:
            raise RevisionConflict("This saved version is part of a monthly update. Reopen the month to edit its latest version.")
        return draft, False
    # The existing nullable-key monthly constraint protects new months too.
    # Compatibility creation keys can no longer allocate additional same-month rows.
    return drafts.monthly_slots().get_or_create(organization=organization, month=month)


@transaction.atomic
def run_update(run, month, *, create=False):
    """Resolve worker writes to the same month identity as founder saves."""
    # ContentFactoryRun keeps organization in its request, not necessarily a FK.
    organization_id = (run.run_request or {}).get("organization_id")
    query = MonthlyUpdateDraft.objects.filter(organization_id=organization_id)
    update_id = (run.run_request or {}).get("update_id")
    if update_id:
        draft = query.filter(pk=update_id).first()
        if draft is None or draft.month != month:
            raise RevisionConflict("This run targets a different update or reporting period.")
        current = latest_monthly_draft(query, month)
        if current is not None and current.pk != draft.pk:
            raise RevisionConflict("A newer monthly update exists. Reopen the month before generating again.")
        return draft
    if create:
        Organization.objects.select_for_update().get(pk=organization_id)
    current = latest_monthly_draft(query, month)
    if current is not None or not create:
        return current
    return query.monthly_slots().get_or_create(organization_id=organization_id, month=month)[0]


def parse_generation_date(data, *, update_id):
    """Accept old clients' explicit dates; monthly clients need only a period."""
    if "updateDate" not in data:
        return None
    try:
        return date.fromisoformat(str(data.get("updateDate")))
    except (ValueError, TypeError) as exc:
        raise ValidationError({"updateDate": "Choose a valid update date."}) from exc


def default_generation_date(draft, *, today):
    """Use today's cutoff, or the end of the monthly update's historical month."""
    month = draft.month
    if today < month:
        raise ValidationError({"updateDate": "Choose today or an earlier reporting month."})
    next_month = date(month.year + 1, 1, 1) if month.month == 12 else date(month.year, month.month + 1, 1)
    return min(next_month - timedelta(days=1), today)


def previous_publications(organization, update_id, update_date):
    """Chronology comes from the published revision, even while its date is edited."""
    from startup_updates.monthly_groups import monthly_representatives
    publications = []
    rows = MonthlyUpdateDraft.objects.filter(organization=organization,
        month__lt=update_date.replace(day=1), published_at__isnull=False)
    rows = monthly_representatives(rows, published=True).exclude(pk=update_id).select_related("published_revision")
    for item in rows:
        memo = item.published_revision.structured_memo if item.published_revision_id else item.structured_memo
        value = memo.get("update_date") or (None if item.published_revision_id else (item.update_date.isoformat() if item.update_date else None))
        key = value or item.month.isoformat()[:7]
        if key <= update_date.isoformat():
            publications.append((item, memo, key))
    return sorted(publications, key=lambda entry: (entry[2], entry[0].first_published_at or entry[0].published_at, entry[0].pk), reverse=True)


def narrative_window(organization, draft, update_date, *, requested_start=None, requested_end=None, default_days=None):
    """Pin connector evidence to the update's calendar month, even on later edits."""
    from startup_updates.activity_scope import monthly_source_period
    zone_name = getattr(getattr(organization, "startup_profile", None), "reporting_timezone", "UTC")
    if update_date.replace(day=1) != draft.month:
        raise ValidationError({"updateDate": "Keep the date within this update's reporting month."})
    try:
        period = monthly_source_period(draft.month, timezone_name=zone_name, as_of=timezone.now())
    except ValueError as exc:
        raise ValidationError({"updateDate": str(exc)}) from exc
    start, end = parse_datetime(period["start"]), parse_datetime(period["end"])
    for value, field in ((requested_start, "start"), (requested_end, "end")):
        if not value:
            continue
        parsed = parse_datetime(str(value))
        if parsed is None or timezone.is_naive(parsed):
            raise ValidationError({"narrativePeriod": "Use timestamps with an explicit timezone."})
        if parsed < start or parsed > end:
            raise ValidationError({"narrativePeriod": "Keep connector sources within this update's month."})
        period[field] = parsed.isoformat()
    if parse_datetime(period["start"]) >= parse_datetime(period["end"]):
        raise ValidationError({"narrativePeriod": "Choose a source range with an end after its start."})
    return period


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
