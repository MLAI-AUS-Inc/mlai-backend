"""Authenticated, durable Community source evidence; no provider I/O.

Callback observation never acquires a second Channel. Cohort maintenance and
final reporting begin outside the per-channel receipt fence and lock all fifteen
Channels in primary-key order, after the reader authority cohort where needed.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import logging
import os
import json
import re
import uuid

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from integrations.models import BridgeSyncInbox, CommunityBridgeChannel, CommunityBridgeReceipt, SlackDmMirrorConversation
from . import community_source_codec as codec

logger = logging.getLogger(__name__)
_current = ContextVar("community_authenticated_source", default=None)
FAULTS = {"", "configuration_changed", "cohort_changed", "source_unverified", "worker_gap", "worker_restart",
          "rate_limited", "non_durable_ingress", "clock_anomaly", "invalid_evidence", "late_observation", "no_reader", "reader_unknown", "comparison_unproven"}
EPOCH_FIELDS = {"version", "id", "cohort", "cohort_hash", "config", "mode", "start_ns", "last_ns", "owner",
                "invalid_reason", "revision", "closed_ns", "members_since_ns", "last_fallback_ns", "report",
                "source_binding", "monitor_config", "monitor_reason", "monitor_started_ns", "comparison_covered_until_ns"}


@dataclass(frozen=True)
class AuthenticatedObservation:
    app_id: str
    workspace_id: str
    source_event_id: str
    received_ns: int
    inbox_id: int | None


@dataclass(frozen=True)
class MonitoringContinuity:
    token: str
    checked_ns: int
    continuous: bool


def mode():
    value = getattr(settings, "SLACK_COMMUNITY_SOURCE", "bot")
    return value if type(value) is str and value in {"bot", "both", "members"} else "invalid"


def roles():
    bot = getattr(settings, "MESSAGE_SYNC_SLACK_APP_ID", "")
    member = getattr(settings, "MESSAGE_SYNC_SLACK_USER_APP_ID", "")
    if (not codec.identifier(bot, "A") or not codec.identifier(member, "A") or bot == member):
        return None
    return {bot: "bot", member: "members"}


def authenticated_role(payload):
    context = _current.get()
    configured = roles()
    if (context is None or not configured or context.app_id not in configured
            or context.workspace_id != getattr(settings, "MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID", "")
            or payload.get("api_app_id") != context.app_id
            or payload.get("team_id") != context.workspace_id):
        return None
    return configured[context.app_id]


def retirement_policy(row, payload):
    """Recheck under the exact Channel lock; no receipt key grants authority."""
    role = authenticated_role(payload)
    if mode() != "members" or _current.get() is None:
        return None
    if row is None or row.audience != "community" or row.destination_platform != "buzz":
        return None
    if role is None:
        _invalidate(row, "configuration_changed")
        return "hold"
    from integrations.services import slack_member_access as access
    assessment = access.community_reader_policy_current(row)
    if assessment is None or not assessment.complete or assessment.effective_source == "hold":
        _invalidate(row, "reader_unknown")
        return "hold"
    access.record_community_reader_state(row, assessment)
    return "active" if assessment.effective_source == role else "retire"


def strip_untrusted(payload):
    return {key: value for key, value in payload.items() if key != codec.KEY}


@contextmanager
def authenticated_callback(payload, *, app_id, received_at, inbox_id=None, source_event_id=None):
    """Call only after signature verification, or from the verified inbox row.

    Values are supplied separately from provider JSON. Historical/synthetic
    calls and direct store entry have no context and cannot create evidence.
    """
    context = None
    event_id = source_event_id if source_event_id is not None else payload.get("event_id")
    if (type(app_id) is str and app_id and len(app_id) <= 100
            and payload.get("api_app_id") == app_id
            and type(event_id) is str and event_id and len(event_id) <= 255
            and (payload.get("event_id") == event_id or payload.get("type") == "app_rate_limited")
            and (inbox_id is None or type(inbox_id) is int and inbox_id > 0)):
        context = AuthenticatedObservation(app_id, str(payload.get("team_id") or ""), event_id,
                                           codec.stamp(received_at), inbox_id)
    token = _current.set(context)
    try:
        yield
    finally:
        _current.reset(token)


def _state(row):
    return row.room_state if isinstance(row.room_state, dict) else {}


def room_identity(row):
    return {"row_id": row.pk, "workspace_id": row.slack_workspace_id,
            "channel_id": row.slack_channel_id, "destination_id": row.destination_channel_id}


def public_room(row):
    return bool(row is not None and row.audience == "community" and row.enabled
                and row.destination_platform == "buzz" and codec.room_valid(room_identity(row))
                and row.slack_workspace_id == getattr(settings, "MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID", "")
                and row.slack_visibility == "public" and row.slack_seen_at is not None
                and not row.slack_archived and not row.slack_deleted
                and row.mirror_decision == "mirror" and not row.hold_reason)


def _rows(*, lock=False):
    query = CommunityBridgeChannel.objects.filter(
        audience="community", destination_platform="buzz",
        slack_workspace_id=getattr(settings, "MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID", ""),
    ).order_by("pk")
    if lock:
        query = query.select_for_update()
    return list(query[:16])


def _source_binding():
    configured = roles()
    if not configured:
        return None
    return codec.digest(b"mlai:community-source-binding:v1\0", [
        sorted(configured.items()), getattr(settings, "MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID", ""),
        getattr(settings, "SLACK_MEMBER_CHANNEL_ALLOWED_IDS", ""),
        bool(getattr(settings, "MESSAGE_SYNC_ENABLED", False)),
        bool(getattr(settings, "SLACK_MEMBER_CHANNELS_ENABLED", False)),
        bool(getattr(settings, "SLACK_OWNER_INVENTORY_ENABLED", False)),
        bool(getattr(settings, "SLACK_COMMUNITY_SEND_AS_MEMBER", False)),
    ])


def _config():
    binding = _source_binding()
    release = getattr(settings, "APP_RELEASE", "") or os.environ.get("APP_RELEASE", "")
    if binding is None or type(release) is not str or re.fullmatch("[a-f0-9]{40}", release) is None:
        return None
    return codec.digest(b"mlai:community-source-config:v1\0", [release, binding])


def _eligible_cohort(rows):
    from .member_channels import member_channel_in_scope
    return (len(rows) == 15 and all(public_room(row) and member_channel_in_scope(
        row.slack_workspace_id, row.slack_channel_id) for row in rows)
        and len({row.destination_channel_id for row in rows}) == 15
        and bool(getattr(settings, "MESSAGE_SYNC_ENABLED", False))
        and bool(getattr(settings, "SLACK_MEMBER_CHANNELS_ENABLED", False))
        and bool(getattr(settings, "SLACK_OWNER_INVENTORY_ENABLED", False)))


def epoch(value):
    if (type(value) is not dict or set(value) != EPOCH_FIELDS
            or type(value.get("version")) is not int or value["version"] != 1
            or not codec.uuid_value(value["id"]) or type(value["cohort"]) is not list
            or len(value["cohort"]) != 15 or not all(codec.room_valid(row) for row in value["cohort"])
            or len({row["row_id"] for row in value["cohort"]}) != 15
            or value["cohort_hash"] != codec.digest(b"mlai:community-source-cohort:v1\0", value["cohort"])
            or any(type(value[key]) is not str or not codec.HEX.fullmatch(value[key]) for key in ("config", "owner", "source_binding", "monitor_config"))
            or type(value["mode"]) is not str or value["mode"] not in {"both", "members"}
            or type(value["invalid_reason"]) is not str or value["invalid_reason"] not in FAULTS
            or type(value["monitor_reason"]) is not str or value["monitor_reason"] not in FAULTS
            or any(type(value[key]) is not int or value[key] < 0 for key in
                   ("start_ns", "last_ns", "revision", "closed_ns", "members_since_ns", "last_fallback_ns",
                    "monitor_started_ns", "comparison_covered_until_ns"))
            or value["start_ns"] <= 0 or value["last_ns"] < value["start_ns"]
            or type(value["report"]) is not dict):
        return None
    report = value["report"]
    if report and (set(report) != {"revision", "fingerprint", "cutoff_ns", "status", "N", "M", "invalidated", "supersedes"}
                   or any(type(report[key]) is not int or report[key] < 0 for key in ("revision", "cutoff_ns", "N", "M"))
                   or type(report["fingerprint"]) is not str or not codec.HEX.fullmatch(report["fingerprint"])
                   or type(report["status"]) is not str or report["status"] not in {"pass", "fail", "inconclusive"}
                   or (report["supersedes"] is not None and (type(report["supersedes"]) is not str or not codec.HEX.fullmatch(report["supersedes"])))
                   or type(report["invalidated"]) is not bool):
        return None
    return json.loads(codec.canonical(value))


def _save(row, value):
    row.room_state = {**(row.room_state if isinstance(row.room_state, dict) else {}), codec.EPOCH_KEY: value}
    row.save(update_fields=("room_state", "updated_at"))


def _invalidate(row, reason):
    value = epoch(_state(row).get(codec.EPOCH_KEY))
    if value is not None:
        if value["mode"] == "members":
            # A new monitoring fault cannot rewrite the already closed seven
            # historical days. It removes prospective stability immediately.
            value["monitor_reason"] = reason
            value["members_since_ns"] = 0
        else:
            value["invalid_reason"] = reason
            if value["report"]:
                value["report"]["invalidated"] = True
        _save(row, value)


def hold_reader_source(row):
    """A pending callback removes prospective proof under its single Channel."""
    if _current.get() is None or mode() != "members" or row.audience != "community":
        return
    current = CommunityBridgeChannel.objects.select_for_update().filter(
        pk=row.pk, audience="community", destination_platform="buzz",
        slack_workspace_id=row.slack_workspace_id, slack_channel_id=row.slack_channel_id,
        destination_channel_id=row.destination_channel_id,
    ).first()
    if current is not None:
        _invalidate(current, "reader_unknown")


def note_reader_transition(row, assessment):
    value = epoch(_state(row).get(codec.EPOCH_KEY))
    if value is not None and value["mode"] == "members" and (
            not assessment.complete or assessment.effective_source != "members"):
        value["members_since_ns"] = 0
        value["last_fallback_ns"] = codec.stamp(timezone.now())
        value["monitor_reason"] = "no_reader" if assessment.fallback_reason == "no_reader" else "reader_unknown"
        _save(row, value)


def heartbeat(worker_id, *, now=None):
    """Advance one durable monitor, never infer history across its restart.

    Both-mode faults restart the unfinished comparison window. After cutover,
    its historical coverage/report is retained and a recovered current monitor
    starts prospective stability from NOW after the same full cohort proof.
    """
    now_ns = codec.stamp(now or timezone.now())
    config, binding, source_mode = _config(), _source_binding(), mode()
    if source_mode == "bot" and not CommunityBridgeChannel.objects.filter(room_state__has_key=codec.EPOCH_KEY).exists():
        return 0
    snapshots = _rows()
    if len(snapshots) != 15:
        return 0
    from integrations.services import slack_member_access as access
    with access.community_reader_cohort(snapshots) as readers:
        with transaction.atomic():
            rows = _rows(lock=True)
            if [room_identity(row) for row in rows] != [room_identity(row) for row in snapshots]:
                return 0
            if not _eligible_cohort(rows) or config is None or source_mode not in {"both", "members"}:
                for row in rows:
                    _invalidate(row, "configuration_changed")
                return 0
            cohort = [room_identity(row) for row in rows]
            existing = [epoch(_state(row).get(codec.EPOCH_KEY)) for row in rows]
            current = existing[0]
            owner = codec.digest(b"mlai:community-source-worker:v1\0", str(worker_id))
            historical = (current is not None and all(item is not None and item["id"] == current["id"]
                and item["cohort"] == cohort and item["source_binding"] == binding
                and all(item[key] == current[key] for key in ("config", "start_ns", "closed_ns", "comparison_covered_until_ns"))
                for item in existing))
            if source_mode == "members" and not historical:
                for row in rows:
                    _invalidate(row, "cohort_changed")
                return 0
            if historical and any(now_ns < item["last_ns"] for item in existing):
                for row in rows:
                    _invalidate(row, "clock_anomaly")
                return 0
            same_monitor = (historical and all(all(item[key] == current[key] for key in
                ("owner", "mode", "last_ns", "monitor_config", "monitor_started_ns", "members_since_ns", "last_fallback_ns"))
                for item in existing))
            if (historical and current["owner"] != owner
                    and 0 <= now_ns-current["last_ns"] <= codec.STALE_NS):
                return 0
            clean = (same_monitor and current["owner"] == owner and current["monitor_config"] == config
                and 0 <= now_ns-current["last_ns"] <= codec.STALE_NS
                and not any(item["monitor_reason"] not in {"", "no_reader", "reader_unknown"} for item in existing))
            if source_mode == "both":
                clean = bool(clean and current["mode"] == "both" and not any(item["invalid_reason"] for item in existing))
                if not clean:
                    current = {"version": 1, "id": str(uuid.uuid4()), "cohort": cohort,
                        "cohort_hash": codec.digest(b"mlai:community-source-cohort:v1\0", cohort),
                        "config": config, "source_binding": binding, "mode": "both",
                        "start_ns": now_ns, "last_ns": now_ns, "owner": owner, "invalid_reason": "",
                        "revision": 1, "closed_ns": 0, "members_since_ns": 0, "last_fallback_ns": 0,
                        "report": {}, "monitor_config": config, "monitor_reason": "",
                        "monitor_started_ns": now_ns, "comparison_covered_until_ns": 0}
            else:
                if current["mode"] == "both":
                    if (any(item["invalid_reason"] for item in existing) or not current["report"]
                            or current["report"]["status"] != "pass"
                            or any(item["report"].get("invalidated") for item in existing)
                            or current["comparison_covered_until_ns"] < current["start_ns"]+codec.WEEK_NS):
                        for row in rows:
                            _invalidate(row, "comparison_unproven")
                        return 0
                    current["closed_ns"] = now_ns
                    clean = False
                ready = all(readers.get(row.pk) is not None and readers[row.pk].complete
                            and readers[row.pk].effective_source == "members" for row in rows)
                current["last_fallback_ns"] = max(item["last_fallback_ns"] for item in existing)
                current["members_since_ns"] = (current["members_since_ns"] or now_ns) if ready and clean else now_ns if ready else 0
                if not ready:
                    current["last_fallback_ns"] = now_ns
                current["monitor_reason"] = "" if ready else "no_reader" if all(
                    readers.get(row.pk) is not None and readers[row.pk].complete for row in rows) else "reader_unknown"
                if not clean:
                    current["monitor_started_ns"] = now_ns
            if current["report"] and any(item is not None and item["report"].get("invalidated") for item in existing):
                current["report"]["invalidated"] = True
            continuous = bool(clean and current["mode"] == source_mode)
            current["owner"], current["monitor_config"] = owner, config
            monitor = MonitoringContinuity(codec.digest(b"mlai:community-source-monitor:v1\0", [
                current["id"], owner, config, source_mode]), now_ns, continuous)
            current["mode"], current["last_ns"] = source_mode, now_ns
            for row in rows:
                access.record_community_reader_state(row, readers[row.pk], continuity=monitor)
                _save(row, current)
    return 15


def unavailable_mapping(payload, channel_id):
    """Record a known local coverage break without classifying private data."""
    if authenticated_role(payload) is None or mode() not in {"both", "members"}:
        return
    row = CommunityBridgeChannel.objects.select_for_update().filter(
        slack_workspace_id=getattr(settings, "MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID", ""),
        slack_channel_id=channel_id, audience="community", destination_platform="buzz",
        room_state__has_key=codec.EPOCH_KEY,
    ).first()
    if row is not None:
        _invalidate(row, "source_unverified")


def wake_source_revalidation(payload):
    """Metadata-only wake before callback hints can acquire any Channel."""
    if mode() != "members" or authenticated_role(payload) is None:
        return
    from .member_channel_events import _channel_id, _private_hint, _external
    from integrations.services import slack_member_access as access
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    if event.get("type") not in {"message", "reaction_added", "reaction_removed"} or _private_hint(event) or _external(payload, event):
        return
    row = CommunityBridgeChannel.objects.filter(
        slack_workspace_id=payload.get("team_id"), slack_channel_id=_channel_id(event),
        audience="community", destination_platform="buzz",
    ).first()
    if (row is None or not access.community_source_needs_revalidation(row)
            or SlackDmMirrorConversation.objects.filter(
                slack_workspace_id=row.slack_workspace_id, slack_conversation_id=row.slack_channel_id).exists()):
        return
    from integrations.models import BridgeSyncState
    from integrations.services.message_sync.history import ensure_state
    from integrations.services.message_sync.head_repair import wake_head_locked
    state = BridgeSyncState.objects.select_for_update().filter(public_channel=row).first()
    if state is None:
        seeded = ensure_state(row)
        state = BridgeSyncState.objects.select_for_update().get(pk=seeded.pk)
    if state.workspace_id == row.slack_workspace_id and state.source_channel_id == row.slack_channel_id:
        wake_head_locked(state)


def observe(receipt, channel, payload, normalized, *, own_send=False):
    """Called with Channel→Receipt ownership before duplicate/echo return."""
    context = _current.get()
    if (context is None or context.source_event_id.startswith("sync:")
            or mode() not in {"both", "members"} or not public_room(channel)):
        return False
    value = epoch(_state(channel).get(codec.EPOCH_KEY))
    if value is None or value["source_binding"] != _source_binding() or room_identity(channel) not in value["cohort"]:
        return False
    if context.workspace_id != channel.slack_workspace_id or payload.get("api_app_id") != context.app_id:
        return False
    configured = roles()
    if not configured or context.app_id not in configured:
        return False
    if context.inbox_id is None:
        _invalidate(channel, "non_durable_ingress")
        return False
    from .member_channel_events import _private_hint, _external, _public_verified
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    if (_private_hint(event) or _external(payload, event) or not _public_verified(channel, event)
            or SlackDmMirrorConversation.objects.filter(slack_workspace_id=context.workspace_id,
                slack_conversation_id=channel.slack_channel_id).exists()):
        _invalidate(channel, "source_unverified")
        return False
    if context.received_ns < value["start_ns"]:
        return False
    event_type, subtype = event.get("type"), event.get("subtype", "")
    message = event.get("message") if subtype == "message_changed" else event.get("previous_message") if subtype == "message_deleted" else event
    message = message if isinstance(message, dict) else {}
    try:
        if event_type == "message" and subtype in {"", "bot_message", "thread_broadcast", "file_share", "me_message"}:
            canonical_ts, message_ns = codec.timestamp(event.get("ts"))
            operation = "message"
            comparison_id = codec.digest(b"mlai:community-source-message:v1\0", [context.workspace_id, channel.slack_channel_id, canonical_ts])
            kind = "own_send" if own_send or message.get("user") == getattr(settings, "SLACK_BRIDGE_BOT_USER_ID", "") else "message"
        elif normalized and normalized.get("delivery_type") in {"edit", "delete", "reaction_add", "reaction_remove"}:
            operation = normalized["delivery_type"]
            target = event.get("item", {}).get("ts") if event_type in {"reaction_added", "reaction_removed"} else event.get("deleted_ts") or message.get("ts")
            _, message_ns = codec.timestamp(target)
            from .store import _public_slack_receipt_key
            identity = _public_slack_receipt_key(payload, normalized)
            if identity is None:
                raise ValueError("source_diagnostic_identity")
            comparison_id = codec.digest(b"mlai:community-source-diagnostic:v1\0", identity)
            kind = "diagnostic"
        else:
            return False
    except (TypeError, ValueError, AttributeError):
        _invalidate(channel, "invalid_evidence")
        return False
    if message_ns > context.received_ns:
        _invalidate(channel, "clock_anomaly")
        return False
    if not value["start_ns"] <= message_ns < value["start_ns"] + codec.WEEK_NS:
        return False
    sighting = {"version": 1, "epoch": value["id"], "cohort": value["cohort_hash"],
                "room": room_identity(channel), "comparison_id": comparison_id, "operation": operation,
                "kind": kind, "message_ns": message_ns,
                "own_send_ns": context.received_ns if kind == "own_send" else 0,
                "sources": {configured[context.app_id]: context.received_ns}}
    old_payload = receipt.payload if isinstance(receipt.payload, dict) else {}
    previous = codec.marker(old_payload.get(codec.KEY))
    try:
        merged = codec.merge(previous, sighting)
    except ValueError:
        _invalidate(channel, "invalid_evidence")
        return False
    if merged != previous:
        receipt.payload = {**old_payload, codec.KEY: merged}
        receipt.save(update_fields=("payload", "updated_at"))
        if value["report"]:
            value["report"]["invalidated"] = True
            _save(channel, value)
    return True


def retained_payload(receipt):
    value = codec.marker((receipt.payload or {}).get(codec.KEY)) if isinstance(receipt.payload, dict) else None
    if (value is None or receipt.platform != "slack" or receipt.channel_id is None
            or receipt.channel.audience != "community" or receipt.channel.destination_platform != "buzz"
            or room_identity(receipt.channel) != value["room"]):
        return {}
    return {codec.KEY: value}


def rate_limited(payload):
    """Only global rate notices: caller has not acquired a per-channel lock."""
    context = _current.get()
    if (context is None or authenticated_role(payload) is None
            or payload.get("type") != "app_rate_limited"):
        return
    with transaction.atomic():
        for row in _rows(lock=True):
            _invalidate(row, "rate_limited")


def _reader_status(rows, *, readers=None, now_ns=None):
    """A bounded local snapshot; missing monitoring proof never means zero."""
    from django.utils.dateparse import parse_datetime
    from integrations.services import slack_member_access as access

    now_ns = now_ns or codec.stamp(timezone.now())
    result = []
    for row in rows[:15]:
        assessment = readers.get(row.pk) if readers is not None else (
            access.assess_readers(row.slack_workspace_id, row.slack_channel_id) if public_room(row) else None)
        valid = assessment is not None and assessment.complete and public_room(row)
        saved = _state(row).get(access.COMMUNITY_READER_STATE)
        saved = saved if isinstance(saved, dict) else {}
        value = epoch(_state(row).get(codec.EPOCH_KEY))
        checked = saved.get("monitor_checked_ns")
        monitor_token = codec.digest(b"mlai:community-source-monitor:v1\0", [
            value["id"], value["owner"], value["monitor_config"], value["mode"]]) if value else None
        proven = bool(valid and value is not None and not value["invalid_reason"]
            and value["monitor_config"] == _config() and value["mode"] == mode()
            and value["monitor_reason"] in {"", "no_reader", "reader_unknown"}
            and type(checked) is int and 0 <= now_ns-checked <= codec.STALE_NS
            and saved.get("monitor_token") == monitor_token and saved.get("continuity_proven") is True
            and saved.get("fingerprint") == assessment.fingerprint
            and saved.get("effective_source") == assessment.effective_source
            and saved.get("fallback_reason") == assessment.fallback_reason)

        def duration(name):
            if not proven:
                return None
            try:
                timestamp = parse_datetime(saved.get(name))
                at = codec.stamp(timestamp)
            except (TypeError, ValueError, AttributeError, OverflowError):
                return None
            return (now_ns-at)//10**9 if 0 < at <= checked <= now_ns else None

        result.append({"room_id": row.pk, "eligible_readers": assessment.eligible_count if valid else None,
            "reader_assessment_complete": bool(valid),
            "effective_source": assessment.effective_source if valid else "hold",
            "fallback_reason": assessment.fallback_reason if valid else "unknown",
            "fallback_duration_seconds": duration("fallback_since"),
            "members_stable_duration_seconds": duration("stable_since"),
            "monitor_continuity_proven": proven,
            "monitor_checked_ns": checked if proven else None})
    return result


def status(*, cutoff_ns=None, finalize=False):
    """One consistent report; all eligible callbacks serialize on these Channels.

    Pending qualified workspace/app inbox rows are unclassified censorship,
    possibly unrelated, never a public-message miss or private-room analytics.
    """
    source_mode = mode()
    rows = _rows()
    if len(rows) != 15:
        return {"mode": source_mode, "status": "inconclusive", "reason": "cohort_not_fifteen",
                "reader_status": _reader_status(rows), "cohort_truncated": len(rows) > 15,
                "N": None, "M": None, "miss_ratio": None, "comparison_measured": False}
    if not any(codec.EPOCH_KEY in _state(row) for row in rows):
        return {"mode": source_mode, "status": "inactive", "epoch": None,
                "reader_status": _reader_status(rows), "N": None, "M": None,
                "miss_ratio": None, "comparison_measured": False}
    from integrations.services import slack_member_access as access
    with transaction.atomic():
        if connection.vendor == "postgresql" and not connection.savepoint_ids:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        with access.community_reader_cohort(rows) as readers:
            current_rows = _rows(lock=True)
            first = epoch(_state(current_rows[0]).get(codec.EPOCH_KEY)) if len(current_rows) == 15 else None
            reader_status = _reader_status(current_rows, readers=readers)
            if first is None:
                return {"mode": source_mode, "status": "inconclusive", "reason": "epoch_missing",
                        "reader_status": reader_status, "N": None, "M": None,
                        "miss_ratio": None, "comparison_measured": False}
            values = [epoch(_state(row).get(codec.EPOCH_KEY)) for row in current_rows]
            now_ns = codec.stamp(timezone.now())
            cutoff = cutoff_ns if cutoff_ns is not None else min(value["last_ns"] for value in values if value)
            if type(cutoff) is not int or cutoff < first["start_ns"] or cutoff > now_ns:
                raise ValueError("source_cutoff_invalid")
            bound = all(value is not None and value["id"] == first["id"]
                and value["source_binding"] == _source_binding()
                and value["cohort"] == [room_identity(row) for row in current_rows]
                and all(value[key] == first[key] for key in
                    ("config", "start_ns", "closed_ns", "comparison_covered_until_ns")) for value in values)
            monitor_complete = bool(bound and _eligible_cohort(current_rows) and all(
                value["monitor_config"] == _config() and value["mode"] == source_mode
                and value["monitor_reason"] in {"", "no_reader", "reader_unknown"}
                and all(value[key] == first[key] for key in
                    ("owner", "mode", "last_ns", "monitor_started_ns", "members_since_ns", "last_fallback_ns"))
                and 0 <= now_ns-value["last_ns"] <= codec.STALE_NS for value in values))
            historical_coverage = bool(bound and all(not value["invalid_reason"]
                and value["comparison_covered_until_ns"] >= value["start_ns"]+codec.WEEK_NS for value in values))
            complete = bool(bound and _eligible_cohort(current_rows) and not any(
                value["invalid_reason"] for value in values) and (
                monitor_complete and all(value["last_ns"] >= cutoff for value in values)
                if source_mode == "both" else historical_coverage if source_mode == "members" else False))
            pending = BridgeSyncInbox.objects.filter(app_id__in=list((roles() or {}).keys()),
                workspace_id=current_rows[0].slack_workspace_id, received_at__lt=_datetime(cutoff)).exclude(status="completed")
            from django.db.models import Min
            unknown_count = pending.count()
            oldest = pending.aggregate(value=Min("received_at"))["value"]
            complete = complete and unknown_count == 0
            from django.db.models import F
            evidence = CommunityBridgeReceipt.objects.filter(platform="slack",
                source_channel_id=F("channel__slack_channel_id"),
                channel_id__in=[row.pk for row in current_rows],
                payload__community_source_compare_v1__epoch=first["id"]).order_by("pk").values_list("payload__community_source_compare_v1", flat=True)
            counts = {row.pk: readers[row.pk].eligible_count if readers.get(row.pk) is not None
                      and readers[row.pk].complete and public_room(row) else None for row in current_rows}
            result = codec.report(evidence.iterator(chunk_size=1000), epoch_id=first["id"], cohort=first["cohort"],
                                  start_ns=first["start_ns"], cutoff_ns=cutoff, complete=bool(complete),
                                  reader_counts=counts, partial=True)
            result.update(mode=source_mode, reader_status=reader_status, comparison_measured=True,
                          unclassified_pending_count=unknown_count,
                          oldest_unclassified_received_ns=codec.stamp(oldest) if oldest else None,
                          monitoring_bound_seconds=180, monitoring_is_not_callback_completeness=True,
                          historical_report=first["report"] or None,
                          historical_coverage_certified=historical_coverage,
                          historical_report_current=bool(bound and first["report"] and not any(
                              value["report"].get("invalidated") for value in values if value)),
                          monitor_complete=monitor_complete,
                          monitor_reason=first["monitor_reason"],
                          monitor_started_ns=first["monitor_started_ns"],
                          members_stable_since_ns=first["members_since_ns"] or None,
                          fourteen_days_without_fallback=(source_mode == "members" and monitor_complete
                              and first["members_since_ns"] > 0 and unknown_count == 0
                              and all(readers[row.pk].complete and readers[row.pk].effective_source == "members" for row in current_rows)
                              and now_ns-first["members_since_ns"] >= 2*codec.WEEK_NS))
            if not result["window_complete"]:
                result["reason"] = "seven_full_days_required"
            if finalize:
                certify = (source_mode == "both" and result["window_complete"] and result["complete"]
                           and result["invalid_evidence"] == 0)
                result["historical_coverage_certified"] = historical_coverage or certify
                saved = {"revision": first["report"].get("revision", 0) + 1,
                         "fingerprint": codec.digest(b"mlai:community-source-report:v1\0", result),
                         "cutoff_ns": cutoff, "status": result["status"], "N": result["N"], "M": result["M"], "invalidated": False,
                         "supersedes": first["report"].get("fingerprint")}
                for row, value in zip(current_rows, values):
                    if value is not None:
                        if certify:
                            value["comparison_covered_until_ns"] = max(value["comparison_covered_until_ns"], cutoff)
                        value["report"] = saved
                        _save(row, value)
                result["report_receipt"] = saved
            return result


def _datetime(value):
    from datetime import datetime, timedelta, timezone as utc
    if type(value) is not int or value <= 0 or value % 1000:
        raise ValueError("source_cutoff_invalid")
    return datetime(1970, 1, 1, tzinfo=utc.utc) + timedelta(microseconds=value // 1000)
