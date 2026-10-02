"""Independent monthly updates with stable creation keys and server-owned titles."""
import calendar
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


def monthly_update_title(month, sequence):
    """Return the consistent owner/community label for an update in a month."""
    suffix = f" #{sequence}" if sequence > 1 else ""
    return f"{calendar.month_name[month.month]} update{suffix}"


def saved_month_sequence(draft):
    """Read the server-allocated sequence stored in the draft's memo metadata."""
    value = (getattr(draft, "structured_memo", None) or {}).get("_month_sequence")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def legacy_month_sequences(siblings):
    """Assign unnumbered legacy rows by ID without changing allocated numbers."""
    used = {value for draft in siblings if (value := saved_month_sequence(draft)) is not None}
    next_number = 1
    numbers = {}
    for draft in siblings:
        value = saved_month_sequence(draft)
        if value is None:
            while next_number in used:
                next_number += 1
            value = next_number
            used.add(value)
        numbers[draft.pk] = value
    return numbers


def monthly_identity(draft):
    """Read stable numbering, falling back to creation order for legacy entries."""
    sequence = saved_month_sequence(draft)
    if sequence is None:
        siblings = list(MonthlyUpdateDraft.objects.filter(
            organization_id=draft.organization_id, month=draft.month,
        ).order_by("pk"))
        sequence = legacy_month_sequences(siblings).get(draft.pk, 1)
    return {"monthSequence": sequence, "updateTitle": monthly_update_title(draft.month, sequence)}


def memo_with_month_identity(draft, memo):
    """Overwrite input metadata with the server-owned update identity."""
    return {**(memo or {}), "_month_sequence": monthly_identity(draft)["monthSequence"]}


def allocate_monthly_titles(drafts, month):
    """Persist legacy identities under the existing organization creation lock."""
    siblings = list(drafts.filter(month=month).select_for_update().order_by("pk"))
    numbers = legacy_month_sequences(siblings)
    for draft in siblings:
        if saved_month_sequence(draft) is None:
            draft.structured_memo = {**(draft.structured_memo or {}), "_month_sequence": numbers[draft.pk]}
            draft.title = monthly_update_title(month, numbers[draft.pk])
            draft.save(update_fields=["title", "structured_memo"])
    return max(numbers.values(), default=0)


@transaction.atomic
def resolve_update(organization, *, month, update_id=None, creation_key=None, update_date=None):
    """Edit an explicit ID or allocate an independent retry-safe monthly copy."""
    Organization.objects.select_for_update().get(pk=organization.pk)
    drafts = MonthlyUpdateDraft.objects.filter(organization=organization)
    month = month.replace(day=1)
    if update_id:
        draft = drafts.filter(pk=update_id).first()
        if draft is None:
            raise NotFound("This update does not belong to the selected startup.")
        allocate_monthly_titles(drafts, draft.month)
        draft.refresh_from_db(fields=["title", "structured_memo"])
        return draft, False
    key = None
    if creation_key:
        try:
            key = UUID(str(creation_key))
        except (ValueError, TypeError, AttributeError):
            raise ValidationError({"creationKey": "Use a valid draft creation key."})
        draft = drafts.filter(creation_key=key).first()
        if draft is not None:
            if draft.month != month:
                raise RevisionConflict("This creation key belongs to a different reporting month.")
            allocate_monthly_titles(drafts, month)
            draft.refresh_from_db(fields=["title", "structured_memo"])
            return draft, False
    largest = allocate_monthly_titles(drafts, month)
    if key is None:
        draft = latest_monthly_draft(drafts, month)
        if draft is not None:
            return draft, False
    return drafts.create(
        organization=organization, month=month, creation_key=key,
        update_date=update_date, title=monthly_update_title(month, largest + 1),
        structured_memo={"_month_sequence": largest + 1},
    ), True


@transaction.atomic
def run_update(run, month, *, create=False):
    """Keep worker retries bound to their exact update even when siblings exist."""
    organization_id = (run.run_request or {}).get("organization_id")
    query = MonthlyUpdateDraft.objects.filter(organization_id=organization_id)
    update_id = (run.run_request or {}).get("update_id")
    if update_id:
        draft = query.filter(pk=update_id).first()
        if draft is None or draft.month != month:
            raise RevisionConflict("This run targets a different update or reporting period.")
        return draft
    if not create:
        return latest_monthly_draft(query, month)
    organization = Organization.objects.get(pk=organization_id)
    return resolve_update(organization, month=month)[0]


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
    return {**monthly_identity(draft), "updateId": draft.pk, "creationKey": str(draft.creation_key) if draft.creation_key else None,
            "updateDate": value, "datePrecision": "day" if value else "month",
            "firstPublishedAt": draft.first_published_at.isoformat() if draft.first_published_at else None,
            "narrativePeriod": memo.get("narrative_period")}
