"""Monthly archive projections without deleting dated publications or their revisions."""
from datetime import date
import re

from django.db.models import OuterRef, Subquery
from rest_framework.exceptions import ValidationError


def requested_month(value):
    """Parse an optional reporting month independently of the request date."""
    if value in (None, ""):
        return None
    value = str(value)
    if not re.fullmatch(r"\d{4}-\d{2}(?:-01)?", value):
        raise ValidationError({"month": "Use YYYY-MM or YYYY-MM-01."})
    try:
        parsed = date.fromisoformat(value + "-01" if len(value) == 7 else value)
    except ValueError as exc:
        raise ValidationError({"month": "Use YYYY-MM or YYYY-MM-01."}) from exc
    if parsed.day != 1:
        raise ValidationError({"month": "Use YYYY-MM or YYYY-MM-01."})
    return parsed


def monthly_representatives(queryset, *, published=False):
    """Choose one visible record per startup/month before applying pagination.

    Apply permission/audience filters first: a private sibling must never replace
    an approved community publication. Old records remain available to their owner.
    """
    ordering = ("-published_at", "-pk") if published else ("-updated_at", "-pk")
    latest = queryset.filter(
        organization_id=OuterRef("organization_id"), month=OuterRef("month"),
    ).order_by(*ordering).values("pk")[:1]
    return queryset.filter(pk=Subquery(latest))


def latest_monthly_draft(queryset, month):
    """Return the current working copy, keeping historic siblings read-only."""
    return queryset.filter(month=month.replace(day=1)).order_by("-updated_at", "-pk").first()
