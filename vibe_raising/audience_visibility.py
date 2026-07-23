from django.db.models import Q


AUDIENCE_JUST_ME = "just_me"
AUDIENCE_COMMUNITY = "community"
AUDIENCE_INVESTORS = "investors"
AUDIENCE_VISIBILITY_CHOICES = (
    AUDIENCE_JUST_ME,
    AUDIENCE_COMMUNITY,
    AUDIENCE_INVESTORS,
)
DEFAULT_AUDIENCE_VISIBILITY = [AUDIENCE_JUST_ME]
COMMUNITY_AND_INVESTORS_VISIBILITY = [AUDIENCE_COMMUNITY, AUDIENCE_INVESTORS]


def default_audience_visibility():
    return list(DEFAULT_AUDIENCE_VISIBILITY)


def normalize_audience_visibility(value, *, default=None):
    if default is None:
        default = DEFAULT_AUDIENCE_VISIBILITY

    if value is None or value == "":
        return list(default)

    if isinstance(value, str):
        raw_values = [value]
    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        raise ValueError("Use a list of audience visibility values.")

    normalized = []
    for item in raw_values:
        text = str(item or "").strip().lower().replace("-", "_").replace(" ", "_")
        if text == "private":
            text = AUDIENCE_JUST_ME
        elif text == "investor":
            text = AUDIENCE_INVESTORS
        if text not in AUDIENCE_VISIBILITY_CHOICES:
            raise ValueError(f"Unsupported audience visibility: {item}")
        if text not in normalized:
            normalized.append(text)

    if not normalized:
        return list(default)

    if AUDIENCE_JUST_ME in normalized and len(normalized) > 1:
        raise ValueError("just_me cannot be combined with community or investors.")

    if AUDIENCE_JUST_ME in normalized:
        return [AUDIENCE_JUST_ME]

    ordered = []
    for audience in COMMUNITY_AND_INVESTORS_VISIBILITY:
        if audience in normalized:
            ordered.append(audience)
    return ordered or list(default)


def monthly_update_visibility(draft):
    try:
        return normalize_audience_visibility(getattr(draft, "audience_visibility", None))
    except ValueError:
        return list(DEFAULT_AUDIENCE_VISIBILITY)


def visible_monthly_updates_for_audience(queryset, audience):
    try:
        normalized = normalize_audience_visibility(audience)
    except ValueError as exc:
        raise ValueError("Audience must be community or investors.") from exc
    if len(normalized) != 1 or normalized[0] not in (AUDIENCE_COMMUNITY, AUDIENCE_INVESTORS):
        raise ValueError("Audience must be community or investors.")

    audience = normalized[0]
    dual_visibility = COMMUNITY_AND_INVESTORS_VISIBILITY
    return queryset.filter(
        published_at__isnull=False,
    ).filter(
        Q(audience_visibility=[audience]) | Q(audience_visibility=dual_visibility)
    )
