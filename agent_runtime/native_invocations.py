"""Durable owner mentions, bounded context and one native reply per invocation."""

import json
import re
from datetime import datetime, timedelta, timezone as datetime_timezone
from functools import partial
from uuid import UUID
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from . import native_crypto, native_sources, runtime
from .models import AgentAuditEvent, AgentCredential, AgentGrant, AgentInstallation, AgentInvocation, AgentInvocationReply, AgentOAuthBinding, AgentSubscription
from .policy import AgentPermissionDenied, authorize, canonical_hash, idempotency_key as validate_idempotency_key, validate_grant_base
from .authentication import validate_credential

HEX_ID = re.compile(r"^[0-9a-f]{64}$")


class NativeTriggerRejected(ValueError):
    """A permanent, content-free rejection of one source event, not the page."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def always_admit():
    return getattr(settings, "AGENT_RUNTIME_NATIVE_ALWAYS_ADMIT", False)


def native_day_start(now):
    """Use one accounting day for native admission, replies and owner status."""
    zone = ZoneInfo(settings.AGENT_RUNTIME_NATIVE_LIMIT_TIMEZONE) if always_admit() else datetime_timezone.utc
    return now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)


def native_daily_cap(grant):
    return settings.AGENT_RUNTIME_NATIVE_ADMISSION_DAILY_CAP if always_admit() else grant.daily_task_limit


def _deliberately_revoked(grant, *, now=None):
    """Distinguish explicit account revocation from unavailable/expired consent."""
    from startup_updates.mcp import oauth
    from .unified_authority import PREFIX
    now = now or timezone.now()
    bindings = AgentOAuthBinding.objects.filter(credential__grant_id=grant.pk, client_id__startswith=PREFIX)
    for binding in bindings:
        try:
            cached = oauth.cache.get(oauth.key("grant", binding.client_id[len(PREFIX):]))
            if not isinstance(cached, dict) or cached.get("expiresAt", 0) <= now.timestamp():
                continue
            epoch = oauth.cache.get(oauth.key("epoch",
                f"{grant.installation.owner.pk}:{oauth.grant_subject(cached.get('company_id'))}")) or ""
            if (cached.get("revoked") or cached.get("auth_version") != grant.installation.owner.auth_version
                    or cached.get("revocation_epoch", "") != epoch):
                return True
        except Exception:
            # Missing cache authority never enables delivery. Admission alone
            # may continue, with a reconnect requirement, until consent returns.
            continue
    return False


def _note_blocked(installation, grant, code, *, now):
    cursor = dict(installation.native_cursor)
    if cursor.get("needs") != code:
        runtime.audit("native.admission.blocked", grant=grant, reason=code)
    cursor["needs"] = code
    installation.native_cursor = cursor
    installation.save(update_fields=("native_cursor", "updated_at"))


def validate_invocation(invocation, grant, *, now=None, allow_replied=False, allow_expired=False):
    """Pure destination and generation checks, also used at every dispatch."""
    now = now or timezone.now()
    validate_grant_base(grant, expected_generation=invocation.generation, now=now)
    installation = grant.installation
    if (invocation.grant_id != grant.pk or invocation.installation_id != installation.pk
            or installation.app_id != "openai-dot" or invocation.resource not in grant.resources
            or str(installation.community_id) != native_sources.parse_resource(invocation.resource)[0]
            or not HEX_ID.fullmatch(invocation.owner_public_key)):
        raise AgentPermissionDenied("invocation_inactive")
    allowed = {"pending"} | ({"replied"} if allow_replied else set()) | ({"expired"} if allow_expired else set())
    if invocation.status not in allowed:
        code = {"expired": "invocation_expired", "cancelled": "invocation_cancelled", "replied": "invocation_replied"}.get(invocation.status, "invocation_inactive")
        raise AgentPermissionDenied(code)
    if not allow_expired and invocation.expires_at <= now:
        raise AgentPermissionDenied("invocation_expired")
    if not HEX_ID.fullmatch(invocation.source_event_id) or not HEX_ID.fullmatch(invocation.thread_id):
        raise AgentPermissionDenied("invocation_destination_invalid")
    return invocation


def _current_principal(principal):
    grant = runtime._grant(principal.grant.pk)
    credential = runtime.locked(AgentCredential.objects.all()).select_related("task").get(pk=principal.credential.pk)
    credential.grant = grant
    from .unified_authority import assert_credential_authority
    assert_credential_authority(grant, credential)
    return validate_credential(credential, audience=principal.credential.audience, allowed_kinds=("access",))


def _authorized_invocation(principal, invocation_id, scope, *, allow_replied=False, allow_expired=False, now=None):
    principal = _current_principal(principal)
    try:
        identifier = UUID(str(invocation_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("Invalid invocation.") from exc
    invocation = runtime.locked(AgentInvocation.objects.all()).get(pk=identifier, grant=principal.grant,
                                                                  installation=principal.grant.installation)
    invocation.installation = principal.grant.installation
    validate_invocation(invocation, principal.grant, now=now, allow_replied=allow_replied, allow_expired=allow_expired)
    if scope not in principal.credential.scopes:
        raise AgentPermissionDenied("scope_denied")
    bound = native_sources.current_device(principal.owner, public_key=principal.grant.installation.owner_device.public_key)
    if bound.pk != principal.grant.installation.owner_device_id:
        raise AgentPermissionDenied("owner_device_changed")
    native_sources.current_device(principal.owner, public_key=invocation.owner_public_key)
    checker = lambda owner, action, resource: native_sources.live_access(owner, action, resource, owner_public_key=invocation.owner_public_key)
    authorize(principal.grant, scope, invocation.resource, expected_generation=principal.credential.generation,
              access_checker=checker, now=now)
    return principal, invocation


def payload(invocation, *, now=None):
    now = now or timezone.now()
    return {"invocation_id": str(invocation.pk), "installation_id": str(invocation.installation_id),
        "resource": invocation.resource, "source_event_id": invocation.source_event_id,
        "thread_id": invocation.thread_id, "status": invocation.status, "expires_at": invocation.expires_at.isoformat(),
        "context_policy": "triggering_public_thread_only", "caller_public_key": invocation.owner_public_key,
        "created_at": invocation.created_at.isoformat(),
        "age_seconds": max(0, int((now - invocation.created_at).total_seconds())), "caller_is_owner": True}


def _pending_invocations(principal):
    """Return only currently authorized work; never reveal inaccessible rows."""
    principal = _current_principal(principal)
    if "messages:read" not in principal.credential.scopes:
        raise AgentPermissionDenied("scope_denied")
    grant = principal.grant
    now = timezone.now()
    validate_grant_base(grant, expected_generation=principal.credential.generation, now=now)
    bound = native_sources.current_device(principal.owner, public_key=grant.installation.owner_device.public_key)
    if bound.pk != grant.installation.owner_device_id:
        raise AgentPermissionDenied("owner_device_changed")
    rows = runtime.locked(AgentInvocation.objects.all()).filter(
        grant=grant, installation=grant.installation, generation=principal.credential.generation,
        resource__in=grant.resources, status="pending", expires_at__gt=now, reply__isnull=True,
    ).order_by("created_at", "pk")
    allowed, access = [], {}
    for invocation in rows:
        invocation.installation, invocation.grant = grant.installation, grant
        try:
            validate_invocation(invocation, grant, now=now)
            native_sources.current_device(principal.owner, public_key=invocation.owner_public_key)
            key = (invocation.owner_public_key, invocation.resource)
            if key not in access:
                checker = lambda owner, action, resource: native_sources.live_access(
                    owner, action, resource, owner_public_key=invocation.owner_public_key)
                try:
                    authorize(grant, "messages:read", invocation.resource,
                        expected_generation=principal.credential.generation, access_checker=checker, now=now)
                except (AgentPermissionDenied, PermissionDenied, ValidationError):
                    access[key] = False
                else:
                    access[key] = True
            if access[key]:
                allowed.append(invocation)
        except (AgentPermissionDenied, PermissionDenied, ValidationError):
            continue
    return principal, allowed


@transaction.atomic
def list_pending_invocations(principal, *, limit=20):
    """List oldest authorized owner mentions without disclosing other work."""
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValidationError("Invalid pending invocation limit.")
    _, rows = _pending_invocations(principal)
    return {"invocations": [payload(row) for row in rows[:limit]],
            "pending_count": len(rows), "truncated": len(rows) > limit}


@transaction.atomic
def get_invocation(principal, invocation_id=None):
    """Resolve an explicit request or the oldest authorized pending owner mention."""
    if invocation_id is None:
        _, candidates = _pending_invocations(principal)
        if not candidates:
            raise ValidationError("No pending owner mention is available.", code="no_pending_invocation")
        invocation = candidates[0]
        result = {**payload(invocation), "pending_count": len(candidates), "more_pending": len(candidates) > 1}
    else:
        _, invocation = _authorized_invocation(principal, invocation_id, "messages:read", allow_replied=True)
        result = payload(invocation)
    runtime.audit("native.invocation.fetched", grant=invocation.grant,
                  invocation_id=str(invocation.pk), tool="get_invocation")
    return result


@transaction.atomic
def read_invocation_thread(principal, invocation_id, *, cursor="", limit=100):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValidationError("Invalid context limit.")
    _, invocation = _authorized_invocation(principal, invocation_id, "messages:read")
    result = native_sources.read_invocation(invocation, cursor=cursor, limit=limit)
    runtime.audit("native.invocation.fetched", grant=invocation.grant,
                  invocation_id=str(invocation.pk), tool="read_invocation_thread")
    return result


def reply_event(invocation, text, *, created_at, now=None):
    now = now or datetime.fromtimestamp(created_at, tz=datetime_timezone.utc)
    installation = invocation.installation
    context = native_sources.invocation_context(invocation, operation="reply", now=now)
    _, channel = native_sources.parse_resource(invocation.resource)
    tags = [["h", channel], ["e", invocation.thread_id, "", "root"],
        ["e", invocation.source_event_id, "", "reply"],
        ["native_invocation", native_crypto.canonical_json(context)],
        ["native_installation", str(installation.pk)]]
    return native_crypto.sign_event(native_crypto.unseal_key(installation.sealed_signing_key),
                                   kind=9, created_at=created_at, tags=tags, content=text)


@transaction.atomic
def reply_to_invocation(principal, invocation_id, text, idempotency_key):
    now = timezone.now()
    if (not isinstance(text, str) or not text.strip() or len(text) > 12000 or len(text.encode()) > 16384
            or "\x00" in text):
        raise ValidationError("Reply must contain bounded plain text.")
    key = validate_idempotency_key(idempotency_key)
    native_sources.require_native(reply=True)
    _, invocation = _authorized_invocation(principal, invocation_id, "messages:reply", allow_replied=True, now=now)
    digest = canonical_hash({"invocation_id": str(invocation.pk), "text": text})
    existing = AgentInvocationReply.objects.filter(invocation=invocation).first()
    if existing:
        if existing.payload_hash != digest or existing.idempotency_key != key:
            raise ValidationError("This invocation already has a different reply.", code="reply_conflict")
        return reply_payload(existing)
    if invocation.status != "pending":
        raise AgentPermissionDenied("invocation_replied")
    sent_today = AgentInvocationReply.objects.filter(invocation__installation=invocation.installation,
        created_at__gte=native_day_start(now)).exclude(status="cancelled").count()
    if sent_today >= native_daily_cap(invocation.grant):
        raise AgentPermissionDenied("daily_limit_reached")
    row = AgentInvocationReply.objects.create(invocation=invocation, idempotency_key=key, payload_hash=digest,
        event=reply_event(invocation, text, created_at=int(now.timestamp()), now=now))
    runtime.audit("native.reply.queued", grant=invocation.grant, invocation_id=str(invocation.pk), event_id=row.event["id"])
    # No network side effect before this transaction commits the exact event.
    return reply_payload(row)


def reply_payload(row):
    return {"invocation_id": str(row.invocation_id), "status": row.status,
            "event_id": row.event.get("id"), "receipt": row.receipt, "error_code": row.error_code}


@transaction.atomic
def get_invocation_status(principal, invocation_id):
    _, invocation = _authorized_invocation(principal, invocation_id, "messages:read", allow_replied=True, allow_expired=True)
    row = AgentInvocationReply.objects.filter(invocation=invocation).first()
    return {**payload(invocation), "reply": reply_payload(row) if row else None}


def invocation_deliverable(grant, invocation_id, *, expected_resource=None, now=None):
    now = now or timezone.now()
    try:
        native_sources.require_native()
        invocation = AgentInvocation.objects.get(pk=invocation_id, grant=grant, installation=grant.installation)
        if expected_resource is not None and invocation.resource != expected_resource:
            return False
        validate_invocation(invocation, grant, now=now)
        if grant.installation.owner_device is None:
            return False
        bound = native_sources.current_device(grant.installation.owner, public_key=grant.installation.owner_device.public_key)
        native_sources.current_device(grant.installation.owner, public_key=invocation.owner_public_key)
        return bound.pk == grant.installation.owner_device_id
    except (AgentInvocation.DoesNotExist, AgentPermissionDenied, PermissionDenied, ValidationError, ValueError):
        return False


def admit_metadata(grant, metadata, *, now=None):
    """Called inside the grant and native cursor transaction; ingress is trusted relay metadata."""
    now = now or timezone.now()
    always = always_admit()
    installation = grant.installation
    if not isinstance(metadata, dict):
        if always:
            raise NativeTriggerRejected("trigger_invalid")
        raise ValidationError("Invalid native trigger.")
    community, channel = native_sources.parse_resource(grant.resources[0])
    if (not HEX_ID.fullmatch(str(metadata.get("author_pubkey", "")))
            or str(metadata.get("community_id")) != community or str(metadata.get("channel_id")) != channel
            or not HEX_ID.fullmatch(str(metadata.get("event_id", "")))
            or not HEX_ID.fullmatch(str(metadata.get("root_event_id", "")))
            or (not always and type(metadata.get("created_at")) is not int)):
        if always:
            raise NativeTriggerRejected("trigger_mismatch")
        raise PermissionDenied("Native trigger does not match the installation.")
    try:
        caller = native_sources.current_device(installation.owner, public_key=metadata["author_pubkey"])
    except PermissionDenied as exc:
        if always:
            raise NativeTriggerRejected("caller_not_current_device") from exc
        raise
    if always:
        if type(metadata.get("created_at")) is not int:
            raise NativeTriggerRejected("invalid_timestamp")
        try:
            raw = datetime.fromtimestamp(metadata["created_at"], tz=datetime_timezone.utc)
        except (ValueError, OverflowError, OSError) as exc:
            raise NativeTriggerRejected("invalid_timestamp") from exc
        if raw > now + timedelta(seconds=960):
            raise NativeTriggerRejected("future_timestamp")
        occurred = min(raw, now)
        if raw < grant.created_at - timedelta(seconds=300):
            raise NativeTriggerRejected("before_current_grant")
        expires = min(grant.expires_at,
            occurred + timedelta(minutes=settings.AGENT_RUNTIME_NATIVE_QUEUE_TTL_MINUTES))
    else:
        occurred = datetime.fromtimestamp(metadata["created_at"], tz=datetime_timezone.utc)
        expires = min(grant.expires_at, occurred + timedelta(minutes=15), now + timedelta(minutes=15))
        if expires <= now or occurred > now + timedelta(minutes=1):
            return None
    existing = AgentInvocation.objects.filter(installation=installation, source_event_id=metadata["event_id"]).first()
    if existing:
        return existing
    if always and expires <= now:
        raise NativeTriggerRejected("older_than_ttl")
    admitted = AgentInvocation.objects.filter(installation=installation,
        created_at__gte=native_day_start(now)).count()
    if admitted >= native_daily_cap(grant):
        # The caller holds the owner/install/grant lock: concurrent admissions
        # cannot spend another slot, and no webhook/inference is triggered.
        return None
    invocation, created = AgentInvocation.objects.get_or_create(installation=installation,
        source_event_id=metadata["event_id"], defaults={"grant": grant, "generation": grant.generation,
            "resource": grant.resources[0], "thread_id": metadata["root_event_id"],
            "owner_public_key": caller.public_key, "expires_at": expires})
    if created:
        from .events import ingest_event
        try:
            ingest_event(source="native", source_event_id=str(invocation.pk), resource=invocation.resource,
                event_type="agent.mentioned", occurred_at=occurred, payload={"installation_id": str(installation.pk),
                    "invocation_id": str(invocation.pk), "source_event_id": invocation.source_event_id}, actor_kind="human")
        except runtime.AgentRuntimeError as exc:
            if always and exc.code == "event_idempotency_conflict":
                raise NativeTriggerRejected("event_conflict") from exc
            raise
        if not always:
            runtime.audit("native.invocation.created", grant=grant, invocation_id=str(invocation.pk))
    return invocation


def _admit_one(grant, event, *, now, listening, needs):
    """Isolate a poison event with a savepoint; page/global failures propagate."""
    source = event.get("event_id", "") if isinstance(event, dict) else ""
    # Audit only a validated identifier, never arbitrary source content.
    source = source if isinstance(source, str) and HEX_ID.fullmatch(source) else ""
    try:
        with transaction.atomic():
            existing = bool(source and AgentInvocation.objects.filter(
                installation=grant.installation, source_event_id=source).exists())
            try:
                invocation = admit_metadata(grant, event, now=now)
            except IntegrityError as exc:
                # Only known event/source uniqueness failures are per-event.
                # Other database failures must roll back the entire page.
                constraint = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", None)
                if constraint in {"agent_invocation_source_unique", "agent_event_source_unique"}:
                    raise NativeTriggerRejected("event_conflict") from exc
                raise
            if invocation is None:
                if not AgentAuditEvent.objects.filter(grant__installation=grant.installation,
                        event_type="native.admission.held", created_at__gte=native_day_start(now)).exists():
                    runtime.audit("native.admission.held", grant=grant, reason="daily_limit_reached")
                return "held"
            if existing:
                return "existing"
            runtime.audit("native.invocation.admitted", grant=grant, invocation_id=str(invocation.pk),
                          source_event_id=source, listening=listening, needs=needs)
            return "created"
    except NativeTriggerRejected as exc:
        runtime.audit("native.invocation.dropped", grant=grant, source_event_id=source,
                      reason=exc.reason, listening=listening)
        return "dropped"


def _poll_problem(installation_id, code, *, now):
    current = AgentInstallation.objects.filter(pk=installation_id).values_list("native_cursor", flat=True).first() or {}
    if always_admit():
        current = {key: value for key, value in current.items() if key != "error_code"}
        current["last_error"] = {"code": code, "at": now.isoformat()}
    else:
        current = {**current, "error_code": code}
    AgentInstallation.objects.filter(pk=installation_id).update(native_cursor=current)


def poll_native_mentions(*, limit=50, now=None):
    now = now or timezone.now()
    try:
        native_sources.require_native()
    except PermissionDenied:
        return 0
    count = 0
    always = always_admit()
    ids = list(AgentInstallation.objects.filter(app_id="openai-dot", is_active=True, state="active")
               .order_by("updated_at", "pk").values_list("pk", flat=True)[:min(limit, 100)])
    for installation_id in ids:
        grant = None
        admitted_count = 0
        try:
            with transaction.atomic():
                grant_id = AgentGrant.objects.filter(installation_id=installation_id, revoked_at__isnull=True,
                    expires_at__gt=now).order_by("-created_at").values_list("pk", flat=True).first()
                if grant_id is None:
                    continue
                grant = runtime._grant(grant_id)
                needs = None
                from .unified_authority import assert_unified_authority
                try:
                    assert_unified_authority(grant, allow_pending=True)
                except AgentPermissionDenied as exc:
                    if not always or exc.code == "unified_agent_disabled" or _deliberately_revoked(grant, now=now):
                        raise
                    needs = "reconnect_required"
                validate_grant_base(grant, now=now)
                installation = grant.installation
                resource = grant.resources[0]
                if len(grant.resources) != 1:
                    raise AgentPermissionDenied("native_pilot_single_channel")
                from .sources import access_checker_for
                authorize(grant, "events:read", resource, access_checker=access_checker_for(grant), now=now)
                device = native_sources.current_device(installation.owner, public_key=installation.owner_device.public_key)
                if device.pk != installation.owner_device_id:
                    if always:
                        _note_blocked(installation, grant, "anchor_device_changed", now=now)
                    continue
                context = {"bot_pubkey": installation.public_key, "installation_id": str(installation.pk),
                           "grant_generation": grant.generation, "limit": 100}
                from community_chat.models import CommunityChatDevice
                caller_keys = CommunityChatDevice.objects.filter(user=installation.owner, status="verified",
                    revoked_at__isnull=True).order_by("pk").values_list("public_key", flat=True).iterator(chunk_size=200)
                cursor = installation.native_cursor
                from .native_mention_scan import scan_mentions
                events, new_cursor = scan_mentions(device.public_key, caller_keys, cursor,
                    partial(native_sources.query, installation.owner, resource, "mentions",
                            owner_public_key=device.public_key, **context))
                active = AgentSubscription.objects.filter(grant=grant, generation=grant.generation,
                    resource=resource, is_active=True, event_types=["agent.mentioned"], delivery__mode="webhook").first()
                from django.utils.dateparse import parse_datetime
                listening_since = parse_datetime(active.delivery.get("listening_since", "")) if active else None
                from .event_webhooks import lease_live
                if not listening_since or not active or not lease_live(active.delivery, now):
                    active = None
                if active:
                    from .unified_authority import assert_record_authority
                    try:
                        assert_record_authority(grant, active.delivery)
                    except AgentPermissionDenied:
                        if not always:
                            raise
                        active = None
                held = False
                # Invalid timestamps remain one-event rejections, rather than
                # causing a mixed-type sort to abort the whole source page.
                ordered = sorted(events, key=lambda e: (
                    e.get("created_at") if isinstance(e, dict) and type(e.get("created_at")) is int else 0,
                    e.get("event_id", "") if isinstance(e, dict) else "")) if always else events
                for event in ordered:
                    if always:
                        outcome = _admit_one(grant, event, now=now, listening=active is not None, needs=needs)
                        admitted_count += outcome == "created"
                        if outcome == "held":
                            held = True
                            break
                        continue
                    # Starting/resuming listening does not activate earlier mentions.
                    if not isinstance(event, dict):
                        raise ValidationError("Native event page is invalid.")
                    if active and type(event.get("created_at")) is int and event["created_at"] >= int(listening_since.timestamp()):
                        if admit_metadata(grant, event, now=now):
                            count += 1
                if not held:
                    if always:
                        if cursor.get("gap"):
                            new_cursor["gap"] = {**cursor["gap"], "rebaselined_at": now.isoformat()}
                            if not cursor["gap"].get("rebaselined_at"):
                                runtime.audit("native.cursor.rebaselined", grant=grant,
                                              epoch=new_cursor["epoch"], sequence=new_cursor["sequence"])
                        if needs:
                            new_cursor["needs"] = needs
                    elif cursor.get("gap_detected"):
                        new_cursor.update(gap_detected=True, error_code="native_cursor_reset_required")
                    installation.native_cursor = new_cursor
                    installation.save(update_fields=("native_cursor", "updated_at"))
            count += admitted_count
        except native_sources.NativeCursorReset:
            if always:
                previous = (grant.installation.native_cursor if grant else {})
                previous = {key: previous.get(key) for key in ("epoch", "sequence")}
                runtime.audit("native.cursor.reset", grant=grant, previous=previous, reason="native_cursor_reset_required")
                AgentInstallation.objects.filter(pk=installation_id).update(native_cursor={
                    "gap": {"previous": previous, "reason": "native_cursor_reset_required", "at": now.isoformat()},
                    "last_error": {"code": "native_cursor_reset_required", "at": now.isoformat()}})
            else:
                AgentInstallation.objects.filter(pk=installation_id).update(
                    native_cursor={"gap_detected": True, "error_code": "native_cursor_reset_required"})
            # Next poll captures a fresh head; acknowledge missed updates by pause/resume.
            continue
        except AgentPermissionDenied as exc:
            if exc.code.startswith("unified_"):
                # Enrolled bots need fresh OAuth consent and an authenticated
                # connection before listening. Lost consent is not a relay outage.
                continue
            _poll_problem(installation_id, "native_source_unavailable", now=now)
            continue
        except (PermissionDenied, ValidationError, native_sources.SourceUnavailable, ValueError):
            # A failure does not advance the durable source cursor.
            _poll_problem(installation_id, "native_source_unavailable", now=now)
            continue
        finally:
            # Round-robin admission includes failed or expired installations.
            AgentInstallation.objects.filter(pk=installation_id).update(updated_at=now)
    return count


def dispatch_native_replies(*, limit=50, now=None):
    supplied_now = now
    now = now or timezone.now()
    try:
        native_sources.require_native(reply=True)
    except PermissionDenied:
        return 0
    sent = 0
    replies = AgentInvocationReply.objects.filter(status__in=("pending", "unknown"), invocation__installation__app_id="openai-dot")
    if getattr(settings, "AGENT_RUNTIME_NATIVE_DURABLE_WATCH", False):
        replies = replies.exclude(invocation__installation__state="paused")
    ids = list(replies.order_by("created_at").values_list("invocation_id", flat=True)[:min(limit, 100)])
    for invocation_id in ids:
        now = supplied_now or timezone.now()
        with transaction.atomic():
            grant_id = AgentInvocation.objects.values_list("grant_id", flat=True).get(pk=invocation_id)
            grant = runtime._grant(grant_id)
            invocation = runtime.locked(AgentInvocation.objects.all()).get(pk=invocation_id)
            invocation.installation, invocation.grant = grant.installation, grant
            row = runtime.locked(AgentInvocationReply.objects.all()).get(invocation=invocation)
            row.invocation = invocation
            if (getattr(settings, "AGENT_RUNTIME_NATIVE_DURABLE_WATCH", False)
                    and grant.installation.state == "paused"):
                continue
            if row.status not in {"pending", "unknown"}:
                continue
            try:
                from .unified_authority import assert_record_authority
                assert_record_authority(grant, row.receipt)
                validate_invocation(invocation, grant, now=now, allow_expired=True)
                bound = native_sources.current_device(grant.installation.owner, public_key=grant.installation.owner_device.public_key)
                if bound.pk != grant.installation.owner_device_id:
                    raise AgentPermissionDenied("owner_device_changed")
                native_sources.current_device(grant.installation.owner, public_key=invocation.owner_public_key)
                checker = lambda owner, action, resource: native_sources.live_access(owner, action, resource, owner_public_key=invocation.owner_public_key)
                authorize(grant, "messages:reply", invocation.resource, expected_generation=invocation.generation,
                          access_checker=checker, now=now)
                # Source ACL checks can take time; use the actual send boundary.
                now = supplied_now or timezone.now()
                if _reply_window_elapsed(row, now):
                    row.save(update_fields=("status", "error_code", "updated_at"))
                    continue
                row.receipt = native_sources.send_reply(row)
                row.status, row.error_code = "sent", ""
                invocation.status = "replied"
                invocation.save(update_fields=("status",))
                sent += 1
            except (AgentPermissionDenied, PermissionDenied) as exc:
                if getattr(exc, "code", None) == "resource_unavailable":
                    continue
                if _reply_window_elapsed(row, supplied_now or timezone.now()):
                    row.save(update_fields=("status", "error_code", "updated_at"))
                    continue
                if row.status == "unknown":
                    row.status, row.error_code = "stopped_unknown", "authority_changed_after_ambiguous_send"
                else:
                    row.status, row.error_code = "cancelled", "authority_changed"
            except (native_sources.SourceUnavailable, ValidationError):
                # Retry the durable same signed event; never create a replacement.
                row.status, row.error_code = "unknown", "relay_receipt_unavailable"
            row.save(update_fields=("status", "receipt", "error_code", "updated_at"))
    return sent


def _signed_context_expiry(row):
    """Read the already signed deadline; malformed context fails closed."""
    try:
        tags = row.event.get("tags", [])
        context_tags = [tag for tag in tags if isinstance(tag, list) and tag and tag[0] == "native_invocation"]
        if not context_tags:
            return row.invocation.expires_at
        if len(context_tags) != 1 or len(context_tags[0]) != 2:
            raise ValueError()
        expiry = json.loads(context_tags[0][1])["expires_at"]
        if type(expiry) is not int:
            raise ValueError()
        return datetime.fromtimestamp(expiry, tz=datetime_timezone.utc)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, OSError):
        return datetime.min.replace(tzinfo=datetime_timezone.utc)


def _reply_window_elapsed(row, now):
    if row.invocation.expires_at > now and _signed_context_expiry(row) > now + timedelta(seconds=5):
        return False
    if row.status == "unknown":
        row.status, row.error_code = "stopped_unknown", "reply_window_elapsed_after_ambiguous_send"
    else:
        row.status = "failed"
        row.error_code = "expired_before_reply" if row.invocation.expires_at <= now else "reply_window_elapsed"
    runtime.audit("native.reply.failed", grant=row.invocation.grant,
                  invocation_id=str(row.invocation.pk), reason=row.error_code)
    return True


def expire_native_invocations(*, limit=200, now=None):
    """Retire elapsed invocations behind the same owner/install/grant fence."""
    try:
        native_sources.require_native()
    except PermissionDenied:
        return 0
    now = now or timezone.now()
    ids = list(AgentInvocation.objects.filter(status="pending", expires_at__lte=now)
               .order_by("expires_at", "pk").values_list("pk", flat=True)[:max(1, min(limit, 200))])
    expired = 0
    for invocation_id in ids:
        with transaction.atomic():
            grant_id = AgentInvocation.objects.values_list("grant_id", flat=True).get(pk=invocation_id)
            grant = runtime._grant(grant_id)
            invocation = runtime.locked(AgentInvocation.objects.all()).get(pk=invocation_id)
            invocation.grant, invocation.installation = grant, grant.installation
            if invocation.status != "pending" or invocation.expires_at > now:
                continue
            row = runtime.locked(AgentInvocationReply.objects.all()).filter(invocation=invocation).first()
            if row and row.status in {"pending", "unknown"}:
                row.invocation = invocation
                if _signed_context_expiry(row) > now:
                    continue
                row.error_code = "expired_after_ambiguous_send" if row.status == "unknown" else "expired_before_reply"
                row.status = "stopped_unknown" if row.status == "unknown" else "failed"
                row.save(update_fields=("status", "error_code", "updated_at"))
            invocation.status = "expired"
            invocation.save(update_fields=("status",))
            runtime.audit("native.invocation.expired", grant=grant, invocation_id=str(invocation.pk))
            expired += 1
    return expired
