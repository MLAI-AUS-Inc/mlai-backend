"""Existing owner consent, provider scopes and membership fence public Slack I/O.

Readers do not require a public attribution link. Posting additionally resolves
the verified device/account and its current Slack attribution link. Credentials
remain inside the existing grant-authority call path and never enter errors.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone

from community_chat.models import CommunityChatDevice
from integrations.models import (
    CommunityBridgeChannel, CommunityBridgeIdentityLink, ExternalServiceConnection,
    SlackChannelMembership, SlackDmMirrorConversation, SlackDmMirrorGrant,
)
from integrations.services import slack_dm_mirror as dm
from integrations.services.message_sync.scheduler import BudgetDeferred
from integrations.services.slack_chat_catalog import public_channels_enabled

PROBE_SECONDS = 600
WORKSPACE_ID = re.compile(r"^T[A-Z0-9]{1,99}$")
CHANNEL_ID = re.compile(r"^C[A-Z0-9]{1,99}$")
PUBLIC_KEY = re.compile(r"^[0-9a-f]{64}$")
METHOD_SCOPES = {
    "conversations_info": frozenset({"channels:read"}),
    "conversations_history": frozenset({"channels:history"}),
    "conversations_replies": frozenset({"channels:history"}),
    "chat_postMessage": frozenset({"chat:write"}),
    "chat_update": frozenset({"chat:write"}),
    "chat_delete": frozenset({"chat:write"}),
    "reactions_add": frozenset({"reactions:write"}),
    "reactions_remove": frozenset({"reactions:write"}),
}
INFO_METHODS = frozenset({"conversations_info"})
HISTORY_METHODS = frozenset({"conversations_history", "conversations_replies"})
SAFE_PROVIDER_ERRORS = dm.SLACK_AUTH_ERROR_CODES | {
    "channel_not_found", "not_in_channel", "is_archived", "ratelimited",
    "restricted_action", "method_not_supported_for_channel_type", "request_timeout",
}


class MemberAccessDenied(dm.SlackDmMirrorAuthorizationError):
    """The current member authority cannot perform this operation."""


def enabled() -> bool:
    """Keep all new member-provider activity behind the deployment switch."""
    return bool(getattr(settings, "SLACK_MEMBER_CHANNELS_ENABLED", False)
                and getattr(settings, "SLACK_OWNER_INVENTORY_ENABLED", False)
                and getattr(settings, "MESSAGE_SYNC_ENABLED", False))


PUBLIC_READER_SCOPES = frozenset({"channels:read", "channels:history"})
MAX_READER_USERS = 2000
COMMUNITY_READER_STATE = "community_reader_v1"
_community_policy = ContextVar("community_reader_policy", default=None)


@dataclass(frozen=True)
class ReaderAssessment:
    """Current local read entitlement, separate from scheduling availability.

    Grants are internal snapshots, never an I/O lease or exported credentials.
    Each MLAI owner counts once; the database separately makes Slack subjects
    unique per workspace. Unknown authority/membership is never a zero proof.
    """

    eligible_count: int
    unknown_count: int
    complete: bool
    effective_source: str
    fallback_reason: str
    fingerprint: str
    eligible_grants: tuple = field(default=(), repr=False)
    available_grants: tuple = field(default=(), repr=False)


def _reader_permission(grant, *, authority=None, allow_expired=False):
    from .slack_chat_catalog import PRIVATE_CHANNEL_CONSENTS

    try:
        if (grant.status != "active" or grant.revoked_at is not None or not grant.user.is_active
                or grant.consent_version not in PRIVATE_CHANNEL_CONSENTS | {SlackDmMirrorGrant.CONSENT_VERSION}
                or grant.connection.provider != "slack"
                or grant.connection.status not in {"connected", "syncing"}):
            return "ineligible", None
        scopes = grant.connection.scopes
        if not isinstance(scopes, (list, tuple, set, frozenset)) or not all(isinstance(s, str) for s in scopes):
            return "unknown", None
        if not PUBLIC_READER_SCOPES.issubset({s.strip() for s in scopes}):
            return "ineligible", None
        if (grant.consented_at is None or timezone.is_naive(grant.consented_at)
                or not allow_expired and grant.connection.token_expires_at is not None
                and grant.connection.token_expires_at <= timezone.now()):
            return "unknown", None
        authority = authority or dm._capture_slack_grant_api_authority(grant, refresh_token=False)
        if (dm.current_slack_oauth_generation(grant.user_id) != authority.oauth_generation
                or not dm._slack_grant_api_authority_is_current(
                    grant, grant.connection, authority, required_scopes=PUBLIC_READER_SCOPES)):
            return "unknown", None
        return "eligible", authority
    except (AttributeError, TypeError, ValueError, dm.SlackDmMirrorError):
        return "unknown", None


def public_reader_enabled(grant, *, authority=None, allow_expired=False):
    """Existing consent/current OAuth read scopes, without posting permission."""
    return _reader_permission(grant, authority=authority, allow_expired=allow_expired)[0] == "eligible"


def reader_user_ids(workspace_id):
    """Full bounded grant-owner cohort; overflow cannot certify zero readers."""
    ids = set(SlackDmMirrorGrant.objects.filter(slack_workspace_id=workspace_id).order_by(
        "user_id").values_list("user_id", flat=True).distinct()[:MAX_READER_USERS + 1])
    if len(ids) > MAX_READER_USERS:
        raise MemberAccessDenied("Reader authority cohort is incomplete.")
    return ids


def assert_reader_cohort_current(workspace_id, user_ids):
    """Reject a newly discovered owner without taking its User below Channel."""
    from .message_sync.member_callback_authority import CallbackAuthorityChanged

    if not reader_user_ids(workspace_id).issubset(user_ids):
        raise CallbackAuthorityChanged("community_reader_cohort_changed")


def _lock_reader_authority(user_ids, workspace_id, channel_id):
    from .message_sync.member_callback_authority import assert_callback_user_locked

    for user_id in sorted(user_ids):
        assert_callback_user_locked(user_id)
    users = list(get_user_model().objects.select_for_update().filter(pk__in=user_ids).order_by("pk"))
    grants = list(SlackDmMirrorGrant.objects.select_for_update().filter(user_id__in=user_ids).order_by("user_id", "pk"))
    connections = list(ExternalServiceConnection.objects.select_for_update().filter(
        user_id__in=user_ids, provider="slack").order_by("user_id", "pk"))
    memberships = list(SlackChannelMembership.objects.select_for_update().filter(
        grant__user_id__in=user_ids, slack_workspace_id=workspace_id,
        slack_channel_id__in=(channel_id,) if isinstance(channel_id, str) else channel_id).order_by("grant_id", "pk"))
    return users, grants, connections, memberships


def assess_readers(workspace_id, channel_id, *, locked=None, mode=None):
    """Assess every current owner; exclusions/cooldowns never reduce entitlement.

    With ``locked`` this is evaluated within the caller's ordered authority
    transaction. Without it the result is a status snapshot, not permission to
    fall back or perform provider I/O. No Slack request or token refresh occurs.
    """
    from .community_bridge.member_channels import member_channel_in_scope
    from .slack_member_membership_epoch import membership_floor

    mode = mode or getattr(settings, "SLACK_COMMUNITY_SOURCE", "bot")
    if not enabled() or not member_channel_in_scope(workspace_id, channel_id):
        return ReaderAssessment(0, 1, False, "hold", "", "")
    if locked is None:
        ids = reader_user_ids(workspace_id)
        users = list(get_user_model().objects.filter(pk__in=ids))
        grants = list(SlackDmMirrorGrant.objects.filter(user_id__in=ids).order_by("pk"))
        connections = list(ExternalServiceConnection.objects.filter(user_id__in=ids, provider="slack"))
        memberships = list(SlackChannelMembership.objects.filter(
            grant__user_id__in=ids, slack_workspace_id=workspace_id, slack_channel_id=channel_id))
    else:
        users, grants, connections, memberships = locked
    by_user = {u.pk: u for u in users}
    by_connection = {c.pk: c for c in connections}
    by_grant = {m.grant_id: m for m in memberships
                if m.slack_workspace_id == workspace_id and m.slack_channel_id == channel_id}
    eligible, available, unknown, identities = [], [], 0, []
    for grant in grants:
        if grant.slack_workspace_id != workspace_id:
            continue
        grant.user, grant.connection = by_user.get(grant.user_id), by_connection.get(grant.connection_id)
        permission, authority = _reader_permission(grant)
        state = None
        try:
            state = _membership_state(grant, by_grant.get(grant.pk))
        except Exception:
            # A failed probe-cache read is unknown membership, never zero.
            pass
        unknown += int(permission != "ineligible" and state != "left" and (permission == "unknown" or state is None))
        if permission == "eligible" and state == "joined":
            eligible.append(grant)
            try:
                if not cache.get(_key(grant, channel_id, "history")):
                    available.append(grant)
            except Exception:
                # Cache availability affects scheduling, never consent count.
                pass
        try:
            floor = membership_floor(grant)
        except (AttributeError, TypeError, ValueError):
            floor = None
        identities.append([grant.pk, grant.user_id, grant.connection_id, grant.status,
                           grant.consent_version, str(grant.consented_at), str(grant.revoked_at),
                           grant.slack_user_id, authority.oauth_generation if authority else None,
                           authority.scopes if authority else None, floor.isoformat() if floor else None,
                           permission, state])
    fingerprint = hashlib.sha256(json.dumps([workspace_id, channel_id, mode, identities],
                                            separators=(",", ":")).encode()).hexdigest()
    count = len({g.user_id for g in eligible})
    complete = unknown == 0
    effective = mode if mode in {"bot", "both"} else "members" if count else "hold"
    return ReaderAssessment(count, unknown, complete, effective,
                            "no_reader" if mode == "members" and complete and not count else "",
                            fingerprint, tuple(eligible), tuple(available))


@contextmanager
def reader_authority_fence(workspace_id, channel_id, *, extra_user_ids=()):
    """Hold the full current cohort before State/Job/Channel through a commit."""
    from .message_sync.member_callback_authority import CallbackAuthorityChanged

    if not transaction.get_connection().in_atomic_block:
        raise transaction.TransactionManagementError("Reader authority requires a transaction")
    ids = reader_user_ids(workspace_id) | set(extra_user_ids)
    if len(ids) > MAX_READER_USERS:
        raise MemberAccessDenied("Reader authority cohort is incomplete.")
    locked = _lock_reader_authority(ids, workspace_id, channel_id)
    if not reader_user_ids(workspace_id).issubset(ids):
        raise CallbackAuthorityChanged("community_reader_cohort_changed")
    try:
        yield locked
    finally:
        if not reader_user_ids(workspace_id).issubset(ids):
            raise CallbackAuthorityChanged("community_reader_cohort_changed")


@contextmanager
def community_reader_cohort(rows):
    """Lock a bounded single-workspace report cohort before its Channels.

    Caller verifies its frozen room identities under subsequent Channel locks.
    This context never performs provider I/O or changes any comparison epoch.
    """
    from .message_sync.member_callback_authority import CallbackAuthorityChanged

    with transaction.atomic():
        rows = tuple(rows)
        workspaces = {row.slack_workspace_id for row in rows}
        if not rows or len(rows) > 15 or len(workspaces) != 1:
            raise MemberAccessDenied("Reader report cohort is incomplete.")
        workspace = next(iter(workspaces))
        channels = {row.slack_channel_id for row in rows}
        if len(channels) != len(rows):
            raise MemberAccessDenied("Reader report sources are not distinct.")
        ids = reader_user_ids(workspace)
        locked = _lock_reader_authority(ids, workspace, tuple(sorted(channels)))
        if not reader_user_ids(workspace).issubset(ids):
            raise CallbackAuthorityChanged("community_reader_cohort_changed")
        try:
            yield {row.pk: assess_readers(workspace, row.slack_channel_id, locked=locked) for row in rows}
        finally:
            if not reader_user_ids(workspace).issubset(ids):
                raise CallbackAuthorityChanged("community_reader_cohort_changed")


def _community_callback_channel(payload):
    if getattr(settings, "SLACK_COMMUNITY_SOURCE", "bot") != "members" or not isinstance(payload, dict):
        return None
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    source = item.get("channel") if str(event.get("type") or "").startswith("reaction_") else event.get("channel")
    source = source.get("id") if isinstance(source, dict) else source
    if (payload.get("team_id") != getattr(settings, "MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID", "")
            or payload.get("api_app_id") not in {getattr(settings, "MESSAGE_SYNC_SLACK_APP_ID", ""),
                                               getattr(settings, "MESSAGE_SYNC_SLACK_USER_APP_ID", "")}
            or not CHANNEL_ID.fullmatch(str(source or ""))):
        return None
    return CommunityBridgeChannel.objects.filter(slack_workspace_id=payload["team_id"],
        slack_channel_id=source, audience="community", destination_platform="buzz").first()


def community_reader_user_ids(payload):
    """Predeclare the complete reader cohort before the callback User fence."""
    row = _community_callback_channel(payload)
    return reader_user_ids(row.slack_workspace_id) if row is not None else set()


@contextmanager
def community_reader_policy(payload):
    """Current callback-only authority, entered before any State/Channel locks."""
    row = _community_callback_channel(payload)
    if row is None:
        yield
        return
    with reader_authority_fence(row.slack_workspace_id, row.slack_channel_id) as locked:
        identity = (row.pk, row.slack_workspace_id, row.slack_channel_id, row.destination_platform,
                    row.destination_workspace_id, row.destination_channel_id)
        token = _community_policy.set((identity, locked))
        try:
            yield
        finally:
            _community_policy.reset(token)


def community_source_needs_revalidation(row):
    """Identify an approved Community source whose sole missing proof is age.

    This pure metadata hint may wake an existing durable history job. It grants
    no provider/content authority and does not discover unknown sources; the
    page still proves its complete reader cohort, owner-private exclusion and
    current public INFO under the ordered source/lease fence.
    """
    from .community_bridge.member_audiences import SOURCE_FRESH_SECONDS
    from .community_bridge.member_channels import member_channel_in_scope

    seen, now = row.slack_seen_at, timezone.now()
    return bool(enabled() and getattr(settings, "SLACK_COMMUNITY_SOURCE", "bot") == "members"
                and member_channel_in_scope(row.slack_workspace_id, row.slack_channel_id)
                and row.audience == "community" and row.destination_platform == "buzz"
                and row.destination_workspace_id and row.destination_channel_id
                and row.enabled and row.slack_visibility == "public"
                and not row.slack_archived and not row.slack_deleted
                and isinstance(row.room_state, dict) and row.mirror_decision == "mirror"
                and not row.hold_reason and not row.room_sync_pending and not row.room_sync_error
                and isinstance(seen, datetime) and timezone.is_aware(seen)
                and seen < now - timedelta(seconds=SOURCE_FRESH_SECONDS))


def community_reader_policy_current(row):
    """Return authority only from the server-held ordered callback context."""
    context = _community_policy.get()
    now = timezone.now()
    if (context is None or context[0] != (row.pk, row.slack_workspace_id, row.slack_channel_id,
            row.destination_platform, row.destination_workspace_id, row.destination_channel_id)
            or row.audience != "community" or row.destination_platform != "buzz"
            or not row.enabled or row.slack_visibility != "public" or row.slack_archived or row.slack_deleted
            or row.mirror_decision != "mirror" or row.hold_reason or row.room_sync_pending or row.room_sync_error
            or not row.slack_seen_at or not now - timedelta(seconds=300) <= row.slack_seen_at <= now
            or SlackDmMirrorConversation.objects.filter(slack_workspace_id=row.slack_workspace_id,
                slack_conversation_id=row.slack_channel_id).exists()):
        return None
    users, grants, connections, _ = context[1]
    assert_reader_cohort_current(row.slack_workspace_id, {u.pk for u in users})
    # These Users are already locked; include this callback's own JOIN/LEFT
    # writes without acquiring a newly discovered owner below Channel.
    memberships = list(SlackChannelMembership.objects.filter(grant__user_id__in=[u.pk for u in users],
        slack_workspace_id=row.slack_workspace_id, slack_channel_id=row.slack_channel_id))
    return assess_readers(row.slack_workspace_id, row.slack_channel_id,
                          locked=(users, grants, connections, memberships))


def record_community_reader_state(row, assessment, *, continuity=None):
    """Persist bounded facts; duration requires the monitor's proven chain.

    Callback/history observations never extend monitor coverage. They retain a
    still-fresh unchanged proof, or invalidate duration on a known transition.
    The comparison epoch independently certifies any rollout stability gate.
    """
    from django.utils.dateparse import parse_datetime
    from .community_bridge.community_source import MonitoringContinuity

    if row.audience != "community":
        return
    instant = timezone.now()
    now = instant.isoformat()
    now_ns = int(instant.timestamp()) * 1_000_000_000 + instant.microsecond * 1000
    mode = getattr(settings, "SLACK_COMMUNITY_SOURCE", "bot")
    binding = hashlib.sha256(json.dumps([
        row.pk, row.slack_workspace_id, row.slack_channel_id, row.destination_platform,
        row.destination_workspace_id, row.destination_channel_id, mode,
        getattr(settings, "MESSAGE_SYNC_SLACK_APP_ID", ""),
        getattr(settings, "MESSAGE_SYNC_SLACK_USER_APP_ID", ""),
        getattr(settings, "SLACK_MEMBER_CHANNEL_ALLOWED_IDS", ""), enabled(),
    ], separators=(",", ":")).encode()).hexdigest()
    state = dict(row.room_state or {})
    previous = state.get(COMMUNITY_READER_STATE)
    previous = previous if isinstance(previous, dict) else {}

    def saved_timestamp(name):
        raw = previous.get(name)
        try:
            stamp = parse_datetime(raw) if isinstance(raw, str) else None
        except ValueError:
            stamp = None
        return raw if stamp is not None and timezone.is_aware(stamp) and stamp <= instant else None

    checked = previous.get("monitor_checked_ns")
    prior_fresh = (type(checked) is int and 0 <= now_ns - checked <= 180_000_000_000
                   and previous.get("binding") == binding and previous.get("mode") == mode)
    monitored = (type(continuity) is MonitoringContinuity and type(continuity.checked_ns) is int
                 and 0 <= now_ns - continuity.checked_ns <= 180_000_000_000
                 and type(continuity.token) is str and re.fullmatch(r"[0-9a-f]{64}", continuity.token)
                 and type(continuity.continuous) is bool)
    unchanged = (previous.get("effective_source") == assessment.effective_source
                 and previous.get("fallback_reason") == assessment.fallback_reason
                 and previous.get("complete") is assessment.complete)
    if monitored:
        preserve = bool(prior_fresh and unchanged and continuity.continuous
                        and previous.get("monitor_token") == continuity.token
                        and checked <= continuity.checked_ns)
        checked_ns, token = continuity.checked_ns, continuity.token
        proven = continuity.continuous
        observed = datetime.fromtimestamp(checked_ns // 1_000_000_000, tz=instant.tzinfo).replace(
            microsecond=(checked_ns % 1_000_000_000) // 1000).isoformat()
    else:
        preserve = bool(prior_fresh and unchanged and previous.get("continuity_proven") is True)
        checked_ns = checked if preserve else None
        token = previous.get("monitor_token") if preserve else None
        proven = preserve
        observed = now
    fallback = assessment.fallback_reason == "no_reader"
    stable = (mode == "members" and assessment.complete and assessment.effective_source == "members"
              and assessment.eligible_count > 0)
    state[COMMUNITY_READER_STATE] = {
        "version": 1, "mode": mode, "binding": binding,
        "eligible_count": assessment.eligible_count, "unknown_count": assessment.unknown_count,
        "complete": assessment.complete, "effective_source": assessment.effective_source,
        "fallback_reason": assessment.fallback_reason,
        "fallback_since": (saved_timestamp("fallback_since") or observed) if fallback and preserve else observed if fallback and monitored else None,
        "last_fallback_at": now if fallback else saved_timestamp("last_fallback_at"),
        "stable_since": (saved_timestamp("stable_since") or observed) if stable and preserve else observed if stable and monitored else None,
        "monitor_token": token, "monitor_checked_ns": checked_ns, "continuity_proven": proven,
        "last_checked_at": now, "fingerprint": assessment.fingerprint,
    }
    row.room_state = state
    row.save(update_fields=("room_state", "updated_at"))
    if not unchanged:
        from .community_bridge.observability import compare
        compare(row, kind="fallback", outcome=(
            "no_reader" if assessment.complete and assessment.fallback_reason == "no_reader"
            else assessment.effective_source if assessment.complete else "hold"))
    if continuity is None:
        # History/callback fallback between monitor ticks must invalidate the
        # prospective stability window immediately. The typed heartbeat owns
        # its own epoch update; this local hook acquires no further locks.
        from .community_bridge.community_source import note_reader_transition
        note_reader_transition(row, assessment)


def _key(grant, channel_id, purpose):
    """Bind local observations to consent/OAuth identity without token material."""
    authority = dm._capture_slack_grant_api_authority(grant, refresh_token=False)
    identity = (authority.grant_id, authority.user_id, authority.connection_id,
                authority.workspace_id, authority.slack_user_id,
                authority.consent_generation, authority.consent_version,
                authority.oauth_generation, authority.scopes, channel_id, purpose)
    return "slack-member:" + hashlib.sha256(repr(identity).encode()).hexdigest()


def _membership(grant, channel_id, *, locked=False):
    rows = SlackChannelMembership.objects.filter(
        grant_id=grant.pk, slack_workspace_id=grant.slack_workspace_id,
        slack_channel_id=channel_id,
    )
    return (rows.select_for_update() if locked else rows).first()


def _membership_state(grant, row):
    from .slack_member_membership_epoch import membership_floor

    floor = membership_floor(grant)
    if row is None or row.observed_at is None or floor is None or row.observed_at < floor:
        return None
    if row.source == "probe":
        if row.observed_at < timezone.now() - timedelta(seconds=PROBE_SECONDS):
            return None
        # A probe under an old OAuth/consent epoch cannot authorize a new one.
        if cache.get(_key(grant, row.slack_channel_id, "membership")) != row.state:
            return None
    return row.state if row.state in {"joined", "left"} else None


class MemberSlackClient:
    """Scope-checking facade over the existing fenced, user-app budgeted SDK."""

    def __init__(self, grant):
        if (not enabled() or not WORKSPACE_ID.fullmatch(str(grant.slack_workspace_id or ""))
                or not public_reader_enabled(grant, allow_expired=True)):
            raise MemberAccessDenied("Member Slack access is unavailable.")
        self._authority = dm._capture_slack_grant_api_authority(grant)
        self._posting_public_key = getattr(grant, "_member_posting_public_key", None)

    def __repr__(self):
        return f"MemberSlackClient(grant_id={self._authority.grant_id})"

    def __getattr__(self, method):
        if method not in METHOD_SCOPES:
            raise AttributeError(method)
        return lambda **kwargs: self.call(method, **kwargs)

    def call(self, method, **kwargs):
        """Recheck live consent and joined authority immediately before I/O."""
        method = method.replace(".", "_")
        scopes = METHOD_SCOPES.get(method)
        if not enabled() or scopes is None or not scopes.issubset(self._authority.scopes):
            raise MemberAccessDenied("The Slack operation lacks current permission.")
        channel_id = kwargs.get("channel")
        from .community_bridge.member_channels import member_channel_in_scope
        if (not WORKSPACE_ID.fullmatch(self._authority.workspace_id)
                or not CHANNEL_ID.fullmatch(str(channel_id or ""))
                or not member_channel_in_scope(self._authority.workspace_id, channel_id)):
            raise MemberAccessDenied("A valid Slack channel is required.")
        grant = None
        started_at = None
        try:
            with transaction.atomic():
                grant, connection = dm._lock_slack_grant_api_authority(
                    self._authority, required_scopes=scopes,
                )
                grant.connection = connection
                permitted = public_reader_enabled if method in INFO_METHODS | HISTORY_METHODS else public_channels_enabled
                if not permitted(grant, authority=self._authority):
                    raise MemberAccessDenied("Member Slack consent is unavailable.")
                if method not in INFO_METHODS | HISTORY_METHODS and (
                    not self._posting_public_key
                    or not _poster_identity_current(grant, self._posting_public_key)
                ):
                    raise MemberAccessDenied("A current verified posting identity is required.")
                if method not in INFO_METHODS and _membership_state(
                    grant, _membership(grant, channel_id, locked=True),
                ) != "joined":
                    raise MemberAccessDenied("Current channel membership is required.")
                started_at = timezone.now()
                response = dm._call_slack_with_grant_authority(
                    self._authority, method, required_scopes=scopes, **kwargs,
                )
                if method == "conversations_info" and not _public_channel_response(response, channel_id):
                    raise MemberAccessDenied("The response is not an eligible public source.")
                return response
        except dm.SlackApiError as error:
            if grant is not None:
                purpose = "history" if method in HISTORY_METHODS else "info" if method in INFO_METHODS else "write"
                record_reader_failure(
                    grant, error, channel_id=channel_id, purpose=purpose, authority=self._authority,
                    observed_at=started_at,
                )
            raise


def member_client(grant) -> MemberSlackClient:
    """Return a client whose every supported method checks its own scopes."""
    return MemberSlackClient(grant)


def _current_info_hint(grant, hint):
    """A callback hint may schedule INFO only within the current consent epoch."""
    from .community_bridge.member_channels import InfoReaderHint, info_reader_hint_reason
    from .slack_member_membership_epoch import membership_floor

    if (not isinstance(hint, InfoReaderHint) or grant.pk != hint.grant_id
            or info_reader_hint_reason(hint.grant_id, hint.observed_at) is None):
        return False
    floor = membership_floor(grant)
    return bool(floor is not None and floor <= hint.observed_at <= timezone.now() + timedelta(minutes=5))


def hinted_info_reader(workspace_id, channel_id, hint):
    """Resolve one untrusted INFO preference before any lower-order room lock."""
    from .community_bridge.member_channels import InfoReaderHint, member_channel_in_scope

    if not isinstance(hint, InfoReaderHint) or not enabled() or not member_channel_in_scope(workspace_id, channel_id):
        return None
    grant = SlackDmMirrorGrant.objects.filter(
        pk=hint.grant_id, slack_workspace_id=workspace_id, status="active", revoked_at__isnull=True,
        user__is_active=True,
    ).select_related("user", "connection").first()
    if grant is None or not _current_info_hint(grant, hint) or not public_reader_enabled(grant, allow_expired=True):
        return None
    return grant


def info_reader_deferred(grant, channel_id):
    """Inspect temporary INFO scheduling state before taking a room lock."""
    return bool(cache.get(_key(grant, channel_id, "info")))


def reader_for_channel(workspace_id, channel_id, *, purpose="history", exclude_grant_ids=(), info_reader_hint=None):
    """Select a reader; only public info may fall back to an unjoined grant.

    member_client records provider failures using its pre-request authority.
    Callers add the failed grant ID to exclude_grant_ids before retrying.
    BudgetDeferred is scheduling, not a grant failure. Selection makes no
    Slack requests and grants no room membership; the client fences history.
    """
    if purpose not in {"history", "info"}:
        raise ValueError("Reader purpose must be history or info")
    from .community_bridge.member_channels import member_channel_in_scope
    if not enabled() or not member_channel_in_scope(workspace_id, channel_id):
        return None
    excluded = set(exclude_grant_ids)
    grants = SlackDmMirrorGrant.objects.filter(
        slack_workspace_id=workspace_id, status="active", revoked_at__isnull=True,
        user__is_active=True,
    ).select_related("user", "connection")
    by_id = {grant.pk: grant for grant in grants}
    memberships = list(SlackChannelMembership.objects.filter(
        slack_workspace_id=workspace_id, slack_channel_id=channel_id,
        state="joined", grant_id__in=by_id,
    ).order_by("-observed_at", "grant_id"))
    joined = {row.grant_id: row for row in memberships}
    pointer = CommunityBridgeChannel.objects.filter(
        slack_workspace_id=workspace_id, slack_channel_id=channel_id,
    ).values_list("reader_grant_id", flat=True).first()
    preferred = ([pointer] if pointer in joined else []) + [row.grant_id for row in memberships]
    if purpose == "info":
        hinted = by_id.get(getattr(info_reader_hint, "grant_id", None))
        if hinted is not None and _current_info_hint(hinted, info_reader_hint):
            preferred.append(hinted.pk)
        preferred += sorted(by_id)
    seen = set()
    for grant_id in preferred:
        if grant_id in seen or grant_id in excluded:
            continue
        seen.add(grant_id)
        grant = by_id[grant_id]
        if not public_reader_enabled(grant, allow_expired=True) or cache.get(_key(grant, channel_id, purpose)):
            continue
        if purpose == "history" and _membership_state(grant, joined.get(grant_id)) != "joined":
            continue
        return grant
    return None


def record_reader_failure(grant, error, *, channel_id, authority, purpose="history", observed_at=None):
    """Record content-free failure/exclusion; revoke rejected auth durably.

    A transport error must not cause an unacknowledged write to switch actors.
    Only readers get temporary failover exclusions; write retry/actor binding
    remains the delivery owner's responsibility.
    """
    if isinstance(error, (BudgetDeferred, dm.SlackDmMirrorRateLimited)):
        return
    # Require the pre-request authority explicitly. An old response must never
    # capture and revoke a newly linked credential during failure handling.
    with transaction.atomic():
        try:
            current, connection = dm._lock_slack_grant_api_authority(authority, required_scopes=set())
        except (dm.SlackDmMirrorAuthorizationError, ObjectDoesNotExist):
            return
        current.connection = connection
        if dm._is_slack_auth_error(error):
            # Retain the exact-authority user fence through the full existing
            # privacy revoke, including its nested local transaction.
            dm.revoke_user_grant(current.user)
            return
        raw = str(error.response.get("error") or "") if isinstance(error, dm.SlackApiError) else ""
        code = raw if raw in SAFE_PROVIDER_ERRORS else "request_failed"
        if (code == "not_in_channel" and isinstance(observed_at, datetime)
                and timezone.is_aware(observed_at)):
            row = _membership(current, channel_id, locked=True)
            if row is not None:
                # This request predates a newer observation. Its negative
                # result cannot overwrite that join or its reader diagnostics.
                if row.observed_at > observed_at:
                    return
                from .slack_channel_membership import mark_left

                if mark_left(current, channel_id, observed_at=observed_at,
                             source="probe", authority=authority):
                    # Bind the negative probe to exact OAuth/consent authority
                    # before its after-commit audience reduction runs.
                    cache.set(_key(current, channel_id, "membership"), "left",
                              timeout=PROBE_SECONDS)
        if purpose in {"history", "info"}:
            cache.set(_key(current, channel_id, purpose), True, timeout=60)
        SlackDmMirrorGrant.objects.filter(pk=current.pk, status="active").update(
            last_error=f"member_access:{code}", updated_at=timezone.now(),
        )
        CommunityBridgeChannel.objects.filter(
            slack_workspace_id=current.slack_workspace_id, slack_channel_id=channel_id,
            reader_grant_id=current.pk,
        ).update(reader_grant=None)


def _poster_identity_current(grant, public_key):
    return CommunityChatDevice.objects.select_for_update().filter(
        user_id=grant.user_id, public_key=public_key, status="verified",
        revoked_at__isnull=True, user__is_active=True,
    ).only("pk").first() is not None and CommunityBridgeIdentityLink.objects.select_for_update().filter(
        user_id=grant.user_id, slack_workspace_id=grant.slack_workspace_id,
        slack_user_id=grant.slack_user_id, revoked_at__isnull=True,
    ).only("pk").first() is not None


def _public_channel_response(response, channel_id):
    try:
        raw = response.get("channel")
    except (AttributeError, TypeError):
        return False
    return bool(
        isinstance(raw, dict) and raw.get("id") == channel_id
        and raw.get("is_private") is False and raw.get("is_channel") is True
        and not any(raw.get(key) for key in ("is_im", "is_mpim", "is_group"))
        and not any(raw.get(key) for key in (
            "is_shared", "is_ext_shared", "is_org_shared", "is_pending_ext_shared", "pending_shared",
        ))
    )


def _probe_membership(grant, channel_id, public_key):
    authority = dm._capture_slack_grant_api_authority(grant)
    started = timezone.now()
    response = member_client(grant).conversations_info(channel=channel_id)
    raw = response.get("channel")
    if not _public_channel_response(response, channel_id):
        return None
    if not isinstance(raw.get("is_member"), bool):
        return None
    state = "joined" if raw["is_member"] else "left"
    with transaction.atomic():
        current, connection = dm._lock_slack_grant_api_authority(authority, required_scopes={"channels:read"})
        current.connection = connection
        if not public_channels_enabled(current, authority=authority) or not _poster_identity_current(current, public_key):
            return None
        row = _membership(current, channel_id, locked=True)
        if row is not None and row.observed_at >= started:
            return _membership_state(current, row)
        from .slack_channel_membership import mark_joined, mark_left

        # The request's start, rather than response time, cannot defeat a
        # newer signed leave. The service persists the matching audience wake.
        changed = (mark_joined if state == "joined" else mark_left)(
            current, channel_id, observed_at=started, source="probe",
            authority=authority, public_verified=True,
        )
        if not changed:
            return _membership_state(current, _membership(current, channel_id, locked=True))
        cache.set(_key(current, channel_id, "membership"), state, timeout=PROBE_SECONDS)
    return state


def _poster_for_author(pubkey, workspace_id, channel_id, *, required_scopes=None, strict_membership=False):
    """Resolve a verified author's own grant; unknown membership gets one probe."""
    key = str(pubkey or "").strip().lower()
    from .community_bridge.member_channels import member_channel_in_scope
    if not enabled() or not PUBLIC_KEY.fullmatch(key) or not member_channel_in_scope(workspace_id, channel_id):
        return None
    device = CommunityChatDevice.objects.filter(
        public_key=key, status="verified", revoked_at__isnull=True, user__is_active=True,
    ).first()
    if device is None:
        return None
    link = CommunityBridgeIdentityLink.objects.filter(
        user_id=device.user_id, slack_workspace_id=workspace_id, revoked_at__isnull=True,
    ).first()
    if link is None:
        return None
    grant = SlackDmMirrorGrant.objects.filter(
        user_id=device.user_id, slack_workspace_id=workspace_id,
        slack_user_id=link.slack_user_id, status="active", revoked_at__isnull=True,
    ).select_related("user", "connection").first()
    additional_scopes = set(required_scopes or ())
    if (grant is None or not public_channels_enabled(grant)
            or not additional_scopes.issubset(set(grant.connection.scopes or []))):
        return None
    authority = dm._capture_slack_grant_api_authority(grant)
    with transaction.atomic():
        current, connection = dm._lock_slack_grant_api_authority(authority, required_scopes={"chat:write"} | additional_scopes)
        current.connection = connection
        if not public_channels_enabled(current, authority=authority) or not _poster_identity_current(current, key):
            return None
        state = _membership_state(current, _membership(current, channel_id, locked=True))
    if state is None:
        state = _probe_membership(current, channel_id, key)
        if state is None and strict_membership:
            raise BudgetDeferred(60)
    if state != "joined":
        return None
    # Probes and cached positives remain subject to a final current-device,
    # consent and membership fence. The eventual write checks membership again.
    with transaction.atomic():
        current, connection = dm._lock_slack_grant_api_authority(authority, required_scopes={"chat:write"} | additional_scopes)
        current.connection = connection
        if (not public_channels_enabled(current, authority=authority)
                or not _poster_identity_current(current, key)
                or _membership_state(current, _membership(current, channel_id, locked=True)) != "joined"):
            return None
        current._member_posting_public_key = key
        return current


def poster_for_author(pubkey, workspace_id, channel_id, *, required_scopes=None, strict_membership=False):
    """Return current posting authority, or None after a consent/device race.

    Provider failures and budget deferrals propagate: callers must not silently
    switch the actor of a write after an uncertain upstream result.
    """
    try:
        return _poster_for_author(pubkey, workspace_id, channel_id, required_scopes=required_scopes,
                                  strict_membership=strict_membership)
    except dm.SlackDmMirrorCredentialError:
        if strict_membership:
            raise
        return None
    except dm.SlackDmMirrorAuthorizationError:
        if strict_membership:
            # Once a grant was selected, a denied/malformed provider proof or
            # authority race is not definitive evidence for changing actors.
            raise
        return None
