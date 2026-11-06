"""Route an owner's parked private Slack events before the directory page.

Group-DM and private-channel membership can change independently of message
delivery, so their live callbacks are parked as owner-scoped ciphertext until
discovery re-reads membership. The paced directory sweep reaches a parked
channel only when its five-item page does. Behind
``MESSAGE_SYNC_TARGETED_STAGED_ROUTING`` an owner's discovery turn handles
those channel IDs first: one fresh ``conversations.info`` plus the existing
paged ``conversations.members`` read, under the same grant authority, shared
budgets and discovery lease, then ``_discover_conversation`` and
``_drain_staged_events_for_conversation`` as an explicit open does.

Nothing is routed unless that full re-check passes. Ineligible sources are
discarded or retired exactly as the sweep would; anything uncertain stays
parked for the sweep. Only channel IDs, due times and attempt counts are
persisted here; no Slack payload or message body enters this checkpoint.
"""
import logging
import math
import time
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.db import transaction
from django.utils.dateparse import parse_datetime
from slack_sdk.errors import SlackApiError

from integrations.services.message_sync import telemetry
from integrations.services.message_sync.scheduler import BudgetDeferred, LeaseLost

KEY = "slack_targeted_staged_routing_v1"
MAX_CHANNELS_PER_TURN = 3
MAX_TRACKED_CHANNELS = 256
RETRY_SECONDS = 30
MAX_RETRY_SECONDS = 900
SWEEP_HANDOFF_SECONDS = 300
KIND_HINTS = frozenset({"mpim", "private_channel"})
UNAVAILABLE_CODES = frozenset({"channel_not_found", "not_in_channel"})
# Outcomes reached without a provider request; they never consume the turn.
LOCAL_OUTCOMES = frozenset({"unauthorized", "untyped"})
logger = logging.getLogger(__name__)


def enabled():
    """Targeted routing needs the durable discovery lease and shared budgets."""
    return bool(getattr(settings, "MESSAGE_SYNC_ENABLED", False)
                and getattr(settings, "MESSAGE_SYNC_TARGETED_STAGED_ROUTING", False))


def staged_seconds(value):
    """Parse one parked event's aware ISO timestamp; malformed values are None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = parse_datetime(value)
    except ValueError:
        return None
    if parsed is None or parsed.tzinfo is None:
        return None
    seconds = parsed.timestamp()
    return seconds if math.isfinite(seconds) else None


def parked_channels(cursor):
    """Summarize parked events per channel without decrypting any body."""
    from integrations.services.slack_dm_mirror import PENDING_EVENT_CHECKPOINT_KEY

    raw = cursor.get(PENDING_EVENT_CHECKPOINT_KEY) if isinstance(cursor, dict) else None
    channels = {}
    if not isinstance(raw, list):
        return channels
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("ciphertext"), str) or not item["ciphertext"]:
            continue
        channel_id = item.get("channel_id")
        # Drain matches the stored ID exactly, so never normalize it here.
        if not isinstance(channel_id, str) or not channel_id or channel_id != channel_id.strip() or len(channel_id) > 100:
            continue
        entry = channels.setdefault(channel_id, {"staged_at": None, "kind": None, "events": 0})
        entry["events"] += 1
        staged = staged_seconds(item.get("staged_at"))
        if staged is not None and (entry["staged_at"] is None or staged < entry["staged_at"]):
            entry["staged_at"] = staged
        if item.get("kind") in KIND_HINTS:
            entry["kind"] = item["kind"]
    return channels


def _retry_state(cursor):
    raw = cursor.get(KEY) if isinstance(cursor, dict) else None
    channels = raw.get("channels") if isinstance(raw, dict) else None
    if not isinstance(channels, dict):
        return {}
    return {
        key: {"due": float(value["due"]), "attempts": value["attempts"]}
        for key, value in channels.items()
        if isinstance(key, str) and isinstance(value, dict)
        and type(value.get("due")) in (int, float) and math.isfinite(value["due"])
        and type(value.get("attempts")) is int and 0 <= value["attempts"] <= 1000
    }


def _settle(authority, channel_id, *, delay=None):
    """Clear (``None``), back off (``"backoff"``) or delay one channel's retry.

    Runs under the current authority and lease. A delay is kept only while the
    channel still has parked events; entries for drained channels are pruned.
    """
    from integrations.services import slack_dm_mirror as dm

    with transaction.atomic():
        _, connection = dm._lock_slack_grant_api_authority(authority, required_scopes=dm.DIRECT_DM_SCOPES)
        original = connection.sync_cursor or {}
        cursor = dict(original)
        parked = parked_channels(cursor)
        state = {key: value for key, value in _retry_state(cursor).items() if key in parked}
        previous = state.pop(channel_id, None)
        entry = None
        if delay is not None and channel_id in parked:
            attempts = (previous or {}).get("attempts", 0)
            seconds = min(MAX_RETRY_SECONDS, RETRY_SECONDS * 2 ** min(attempts, 10)) if delay == "backoff" else delay
            entry = {"due": time.time() + max(1, seconds), "attempts": min(1000, attempts + 1)}
        # Over the bound, forget the soonest-due entries: they would retry anyway.
        others = sorted(state.items(), key=lambda item: item[1]["due"])
        keep = MAX_TRACKED_CHANNELS - (entry is not None)
        state = dict(others[max(0, len(others) - keep):])
        if entry is not None:
            state[channel_id] = entry
        if state:
            cursor[KEY] = {"channels": state}
        else:
            cursor.pop(KEY, None)
        if cursor != original:
            connection.sync_cursor = cursor
            connection.save(update_fields=("sync_cursor", "updated_at"))


def _settle_quietly(authority, channel_id, *, delay):
    """Record a retry unless consent already changed; a lost lease still wins."""
    from integrations.services import slack_dm_mirror as dm

    try:
        _settle(authority, channel_id, delay=delay)
    except dm.SlackDmMirrorAuthorizationError:
        pass


def _probe_plan(grant, channel_id, hint):
    """Choose a source read the current consent can authorize.

    Returns ``(decision, required_scopes)``. ``probe`` is safe to send:
    Slack reports ``missing_scope`` as an auth failure that revokes the grant,
    so a type this grant cannot read is never probed. ``unauthorized`` means
    no readable type could match, which the sweep's type filter would also
    never list. ``untyped`` leaves an ambiguous channel to the paced sweep.
    """
    from integrations.services import slack_dm_mirror as dm
    from integrations.services.slack_chat_catalog import CATALOG_KEY

    if channel_id.startswith("D"):
        return "probe", set(dm.DIRECT_DM_SCOPES)
    scopes = set(grant.connection.scopes or [])
    group_ok = dm.GROUP_DM_SCOPES.issubset(scopes)
    private_ok = dm.private_channels_enabled(grant)
    if hint is None:
        catalog = (grant.connection.provider_metadata or {}).get(CATALOG_KEY)
        entry = catalog.get(channel_id) if isinstance(catalog, dict) else None
        hint = entry.get("kind") if isinstance(entry, dict) and entry.get("kind") in KIND_HINTS else None
    if hint == "mpim":
        return ("probe", dm.DIRECT_DM_SCOPES | dm.GROUP_DM_SCOPES) if group_ok else ("unauthorized", None)
    if hint == "private_channel":
        return ("probe", dm.DIRECT_DM_SCOPES | dm.PRIVATE_CHANNEL_SCOPES) if private_ok else ("unauthorized", None)
    if group_ok and private_ok:
        return "probe", dm.DIRECT_DM_SCOPES | dm.GROUP_DM_SCOPES | dm.PRIVATE_CHANNEL_SCOPES
    if not group_ok and not private_ok:
        return "unauthorized", None
    return "untyped", None


def _fresh_source(authority, channel_id, required_scopes):
    """Read current source metadata; ``None`` means the owner cannot see it."""
    from integrations.services import slack_dm_mirror as dm

    try:
        response = dm._call_slack_with_grant_authority(
            authority, "conversations_info", required_scopes=required_scopes, channel=channel_id,
        )
    except SlackApiError as exc:
        if dm._is_slack_auth_error(exc) or dm._slack_retry_after_seconds(exc):
            raise
        try:
            code = str(exc.response.get("error") or "")
        except (AttributeError, TypeError):
            code = ""
        if code in UNAVAILABLE_CODES:
            return None
        raise
    details = response.get("channel") if hasattr(response, "get") else None
    if not isinstance(details, dict) or details.get("id") != channel_id:
        raise dm.SlackDmMirrorUpstreamError("Slack returned a different conversation.")
    return details


def _bounded_details(details):
    """Keep only routing metadata: Slack's info response can contain messages."""
    safe = {key: str(details[key])[:limit] for key, limit in (("id", 100), ("user", 100), ("name", 255))
            if key in details}
    safe.update({key: details[key] for key in (
        "is_im", "is_mpim", "is_private", "is_member", "is_open", "is_archived",
    ) if isinstance(details.get(key), bool)})
    return safe


def _route_channel(grant, authority, channel_id, parked, *, reset_history, profiles, path="targeted"):
    """Revalidate one parked channel and route it only through the full check."""
    from integrations.models import SlackDmMirrorConversationStatus
    from integrations.services import slack_dm_mirror as dm
    from integrations.services.message_sync.recent_history import enabled as recent_first
    from integrations.services.slack_chat_catalog import raw_conversation_kind
    from integrations.services.slack_discovery_progress import conversation_progress

    decision, scopes = _probe_plan(grant, channel_id, parked["kind"])
    if decision == "unauthorized":
        dm._discard_staged_events_for_channel(authority, channel_id)
        return "unauthorized"
    if decision == "untyped":
        _settle(authority, channel_id, delay=SWEEP_HANDOFF_SECONDS)
        return "untyped"
    details = _fresh_source(authority, channel_id, scopes)
    if details is None:
        dm._discard_staged_events_for_channel(authority, channel_id)
        return "unavailable"
    # Same retirements, in the same order, as a directory listing of this source.
    if bool(details.get("is_archived")) and grant.consent_version != dm.ALL_HISTORY_CONSENT:
        dm._retire_ineligible_from_slack_response(
            authority, channel_id, reason=dm.SLACK_CONVERSATION_UNAVAILABLE_REASON,
            required_scopes=dm.DIRECT_DM_SCOPES,
        )
        dm._discard_staged_events_for_channel(authority, channel_id)
        return "archived"
    if dm._is_external_shared_conversation(details):
        dm._retire_ineligible_from_slack_response(
            authority, channel_id, reason=dm.SLACK_CONNECT_INELIGIBLE_REASON,
            required_scopes=dm.DIRECT_DM_SCOPES,
        )
        dm._discard_staged_events_for_channel(authority, channel_id)
        return "slack_connect"
    kind = raw_conversation_kind(details)
    readable = {
        "im": True,
        "mpim": dm.GROUP_DM_SCOPES.issubset(set(grant.connection.scopes or [])),
        "private_channel": dm.private_channels_enabled(grant),
    }
    if not readable.get(kind) or details.get("is_member") is False:
        dm._discard_staged_events_for_channel(authority, channel_id)
        return "ineligible"
    if kind in {"mpim", "private_channel"} and details.get("is_member") is not True:
        # Explicit opens require positive membership too. Without it, deliver
        # nothing and leave the events to the sweep's own membership listing.
        _settle(authority, channel_id, delay=SWEEP_HANDOFF_SECONDS)
        return "member_unknown"
    if (recent_first() and details.get("is_open") is False
            and not grant.conversations.filter(slack_conversation_id=channel_id).exists()):
        # The sweep also declines to create a room for a closed source.
        _settle(authority, channel_id, delay=SWEEP_HANDOFF_SECONDS)
        return "closed"
    # A stable cycle start lets a deferred member/profile page resume next turn.
    started_at = datetime.fromtimestamp(parked["staged_at"] or time.time(), tz=dt_timezone.utc)
    with conversation_progress(authority, channel_id, kind, started_at):
        # Live events bypass the recency probe; history import keeps its window.
        conversation = dm._discover_conversation(
            grant, authority, _bounded_details(details), profile_cache=profiles(),
            force_backfill=False, reset_history=reset_history,
            activity_seconds=dm._slack_conversation_activity_seconds(details),
            recent_activity=True,
        )
    if conversation is None:
        dm._discard_staged_events_for_channel(authority, channel_id)
        return "ineligible"
    if not dm.SlackDmMirrorConversation.objects.filter(
        pk=conversation.pk, status=SlackDmMirrorConversationStatus.LIVE,
    ).exists():
        # Provisioning or the device audience is not ready; keep events parked.
        _settle(authority, channel_id, delay="backoff")
        return "fenced"
    dm._drain_staged_events_for_conversation(authority, conversation.pk, path=path)
    return "routed"


def route_staged_events(grant, authority, *, reset_history=False, limit=MAX_CHANNELS_PER_TURN):
    """Handle the oldest due parked channels before device or directory work.

    Returns True when a provider request was spent on parked channels, so the
    caller ends this owner's turn. Admission deferral of the turn's first
    request returns the unused turn, as explicit opens do; any other deferral
    keeps the channel parked with its own due time and ends the turn.
    """
    if not enabled():
        return False
    from integrations.services import slack_dm_mirror as dm

    cursor = grant.connection.sync_cursor or {}
    parked = parked_channels(cursor)
    if not parked:
        return False
    now = time.time()
    retry = _retry_state(cursor)
    due = sorted(
        (channel_id for channel_id in parked if retry.get(channel_id, {}).get("due", 0) <= now),
        key=lambda channel_id: (parked[channel_id]["staged_at"] or now, channel_id),
    )
    cache = {}

    def profiles():
        if "profiles" not in cache:
            cache["profiles"] = dm._stored_profile_cache(grant)
        return cache["profiles"]

    requested = False
    for channel_id in due[:max(1, limit)]:
        started = time.monotonic()
        try:
            outcome = _route_channel(
                grant, authority, channel_id, parked[channel_id],
                reset_history=reset_history, profiles=profiles,
            )
        except LeaseLost:
            raise
        except Exception as exc:
            if dm._is_slack_auth_error(exc):
                raise
            retry_after = dm._slack_retry_after_seconds(exc)
            if isinstance(exc, (BudgetDeferred, dm.SlackDmMirrorRateLimited)) or retry_after:
                _settle_quietly(authority, channel_id, delay=retry_after or RETRY_SECONDS)
                if (not requested and isinstance(exc, BudgetDeferred)
                        and exc.before_request_method == "conversations.info"):
                    exc.discovery_admission_deferred = True
                raise
            _settle_quietly(authority, channel_id, delay="backoff")
            logger.warning("slack_staged_targeted_retry grant_id=%s error=%s", grant.pk, type(exc).__name__)
            if isinstance(exc, dm.SlackDmMirrorAuthorizationError):
                # Consent or membership changed under this turn. Like the
                # sweep, stop without another request under that authority.
                return True
            requested = True
            continue
        if outcome not in LOCAL_OUTCOMES:
            requested = True
        if outcome not in {"untyped", "member_unknown", "closed", "fenced"} and channel_id in retry:
            _settle(authority, channel_id)
        logger.info(
            "slack_staged_targeted grant_id=%s outcome=%s events=%s elapsed_ms=%s",
            grant.pk, outcome, parked[channel_id]["events"], round((time.monotonic() - started) * 1000),
        )
    return requested


def log_staged_latency(path, grant_id, staged_at_values, *, now=None):
    """Log parked-to-routed timing with only an opaque grant ID and counts."""
    values = [value for value in staged_at_values
              if type(value) in (int, float) and math.isfinite(value)]
    if not values:
        return
    now = time.time() if now is None else now
    elapsed = [max(0, round((now - value) * 1000)) for value in values]
    for milliseconds in elapsed:
        telemetry.record_latency("staged_routed", milliseconds)
    logger.info("slack_staged_routed grant_id=%s path=%s events=%s latency_ms=%s",
                grant_id, path, len(elapsed), max(elapsed))
