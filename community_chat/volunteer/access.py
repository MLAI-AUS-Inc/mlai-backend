"""Fail-closed identity, permissions and source boundaries for Volunteer."""

from datetime import datetime, timedelta
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from roo.permissions import is_points_admin_user


class VolunteerError(ValueError):
    """A stable typed API failure with safe member-facing detail."""

    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def community_id():
    """Derive the boundary from trusted deployment configuration, never a body."""
    return (
        getattr(settings, "COMMUNITY_CHAT_VOLUNTEER_COMMUNITY", "")
        or urlparse(settings.COMMUNITY_CHAT_RELAY_URL).hostname
    )


def flag(name):
    """Read separately gated visibility, submissions, awards and bonuses."""
    return bool(getattr(settings, f"COMMUNITY_CHAT_VOLUNTEER_{name.upper()}", False))


def awards_enabled(action_key):
    """Permit advice and intro rewards without enabling unrelated credits."""
    return flag("awards_enabled") or (
        action_key == "helpful_answer" and flag("advice_rewards_enabled")
    ) or (
        action_key == "introduce_yourself" and flag("intro_rewards_enabled")
    )


def capabilities(user):
    """Resolve account-linked Points Admin authority; rank never grants a role."""
    current = get_user_model().objects.filter(pk=user.pk, is_active=True).first()
    allowed = bool(current and is_points_admin_user(current))
    return dict(
        can_review=allowed,
        can_publish=allowed,
        can_correct=allowed,
        can_request=flag("recognition_enabled"),
    )


def require_capability(user, capability):
    """Enforce a permission at the service boundary as well as presentation."""
    if not capabilities(user).get(capability):
        raise VolunteerError("not_authorised", 403)


def channels():
    """Return maintainer-configured public channel IDs, not names or guesses."""
    return getattr(settings, "COMMUNITY_CHAT_VOLUNTEER_CHANNELS", {})


def public_source(source, *, thread_required=False):
    """Validate references without fetching arbitrary member-supplied URLs."""
    if not isinstance(source, dict) or set(source) - {
        "channel_id",
        "thread_root_id",
        "message_id",
        "source_id",
        "event_id",
        "url",
    }:
        raise VolunteerError("invalid_source")
    result = {}
    for key, value in source.items():
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > (2000 if key == "url" else 255):
            raise VolunteerError("invalid_source")
        result[key] = value.strip()
    channel = result.get("channel_id")
    if channel and channel not in set(channels().values()):
        raise VolunteerError("source_unavailable", 403)
    if thread_required and (not channel or not result.get("thread_root_id")):
        raise VolunteerError("public_thread_required")
    url = result.get("url")
    if url and (
        urlparse(url).scheme != "https"
        or urlparse(url).username
        or urlparse(url).password
    ):
        raise VolunteerError("invalid_source")
    return result


def occurrence(value):
    """Accept timezone-aware source timestamps, never future occurrences."""
    try:
        result = parse_datetime(value) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise VolunteerError("invalid_occurrence") from exc
    if (
        not isinstance(result, datetime)
        or timezone.is_naive(result)
        or result > timezone.now()
    ):
        raise VolunteerError("invalid_occurrence")
    return result


def linked_member(member_id):
    """Return an active canonical account with a verified Chat device."""
    from community_chat.models import CommunityChatDevice

    try:
        user = get_user_model().objects.get(pk=member_id, is_active=True)
    except (ValueError, TypeError, get_user_model().DoesNotExist) as exc:
        raise VolunteerError("member_unavailable", 404) from exc
    if not CommunityChatDevice.objects.filter(user=user, status="verified", revoked_at__isnull=True).exists():
        raise VolunteerError("member_unavailable", 404)
    return user


def actor_for_key(public_key, *, occurred_at=None):
    """Map a current device, optionally proving ownership at source time too."""
    from community_chat.models import CommunityChatDevice

    device = (
        CommunityChatDevice.objects.select_related("user")
        .filter(public_key=public_key, status="verified", revoked_at__isnull=True, user__is_active=True)
        .first()
    )
    if device is None:
        raise VolunteerError("member_unavailable", 404)
    if occurred_at is not None:
        # Signed relay timestamps have second precision. A key may be revoked
        # and enrolled by another account; its latest owner cannot inherit an
        # older introduction. Conflicting owners in one second stay unknown.
        start = occurred_at.replace(microsecond=0)
        end = start + timedelta(seconds=1)
        owners = list(CommunityChatDevice.objects.filter(
            public_key=public_key, status__in=("verified", "revoked"),
            created_at__lt=end, verified_at__lt=end,
        ).filter(Q(revoked_at__isnull=True) | Q(revoked_at__gte=start)).order_by().values_list(
            "user_id", flat=True,
        ).distinct()[:2])
        if owners != [device.user_id]:
            raise VolunteerError("member_unavailable", 404)
    return device.user
