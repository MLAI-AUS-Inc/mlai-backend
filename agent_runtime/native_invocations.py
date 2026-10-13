"""Durable owner mentions, bounded context and one native reply per invocation."""

import re
from datetime import datetime, timedelta, timezone as datetime_timezone
from functools import partial
from uuid import UUID

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from . import native_crypto, native_sources, runtime
from .models import AgentCredential, AgentGrant, AgentInstallation, AgentInvocation, AgentInvocationReply, AgentSubscription
from .policy import AgentPermissionDenied, authorize, canonical_hash, idempotency_key as validate_idempotency_key, validate_grant_base
from .authentication import validate_credential

HEX_ID = re.compile(r"^[0-9a-f]{64}$")


def validate_invocation(invocation, grant, *, now=None, allow_replied=False, allow_expired=False):
    """Pure destination and generation checks, also used at every dispatch."""
    now = now or timezone.now()
    validate_grant_base(grant, expected_generation=invocation.generation, now=now)
    installation = grant.installation
    if (invocation.grant_id != grant.pk or invocation.installation_id != installation.pk
            or installation.app_id != "openai-dot" or invocation.resource not in grant.resources
            or str(installation.community_id) != native_sources.parse_resource(invocation.resource)[0]
            or not HEX_ID.fullmatch(invocation.owner_public_key)
            or invocation.status not in ({"pending", "replied"} if allow_replied else {"pending"})
            or (not allow_expired and invocation.expires_at <= now)):
        raise AgentPermissionDenied("invocation_inactive")
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


def payload(invocation):
    return {"invocation_id": str(invocation.pk), "installation_id": str(invocation.installation_id),
        "resource": invocation.resource, "source_event_id": invocation.source_event_id,
        "thread_id": invocation.thread_id, "status": invocation.status, "expires_at": invocation.expires_at.isoformat(),
        "context_policy": "triggering_public_thread_only", "caller_public_key": invocation.owner_public_key}


@transaction.atomic
def get_invocation(principal, invocation_id=None):
    """Resolve an explicit request or the sole live pending request for this grant."""
    if invocation_id is None:
        principal = _current_principal(principal)
        if "messages:read" not in principal.credential.scopes:
            raise AgentPermissionDenied("scope_denied")
        candidates = list(runtime.locked(AgentInvocation.objects.all()).filter(
            grant=principal.grant, installation=principal.grant.installation,
            generation=principal.credential.generation, resource__in=principal.grant.resources,
            status="pending", expires_at__gt=timezone.now(),
        ).order_by("created_at", "pk").values_list("pk", flat=True)[:2])
        if len(candidates) != 1:
            raise ValidationError("Provide invocation_id unless exactly one live pending owner mention exists.")
        invocation_id = candidates[0]
    _, invocation = _authorized_invocation(principal, invocation_id, "messages:read", allow_replied=True)
    return payload(invocation)


@transaction.atomic
def read_invocation_thread(principal, invocation_id, *, cursor="", limit=100):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValidationError("Invalid context limit.")
    _, invocation = _authorized_invocation(principal, invocation_id, "messages:read")
    return native_sources.read_invocation(invocation, cursor=cursor, limit=limit)


def reply_event(invocation, text, *, created_at):
    installation = invocation.installation
    context = native_sources.invocation_context(invocation, operation="reply")
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
            raise ValidationError("This invocation already has a different reply.")
        return reply_payload(existing)
    if invocation.status != "pending":
        raise PermissionDenied("Invocation is no longer awaiting a reply.")
    sent_today = AgentInvocationReply.objects.filter(invocation__installation=invocation.installation,
        created_at__gte=now.replace(hour=0, minute=0, second=0, microsecond=0)).exclude(status="cancelled").count()
    if sent_today >= invocation.grant.daily_task_limit:
        raise PermissionDenied("Daily reply limit reached.")
    row = AgentInvocationReply.objects.create(invocation=invocation, idempotency_key=key, payload_hash=digest,
        event=reply_event(invocation, text, created_at=int(now.timestamp())))
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
        bound = native_sources.current_device(grant.installation.owner, public_key=grant.installation.owner_device.public_key)
        native_sources.current_device(grant.installation.owner, public_key=invocation.owner_public_key)
        return bound.pk == grant.installation.owner_device_id
    except (AgentInvocation.DoesNotExist, AgentPermissionDenied, PermissionDenied, ValidationError, ValueError):
        return False


def admit_metadata(grant, metadata, *, now=None):
    """Called inside the grant and native cursor transaction; ingress is trusted relay metadata."""
    now = now or timezone.now()
    installation = grant.installation
    if not isinstance(metadata, dict):
        raise ValidationError("Invalid native trigger.")
    community, channel = native_sources.parse_resource(grant.resources[0])
    if (not HEX_ID.fullmatch(str(metadata.get("author_pubkey", "")))
            or str(metadata.get("community_id")) != community or str(metadata.get("channel_id")) != channel
            or not HEX_ID.fullmatch(str(metadata.get("event_id", "")))
            or not HEX_ID.fullmatch(str(metadata.get("root_event_id", "")))
            or type(metadata.get("created_at")) is not int):
        raise PermissionDenied("Native trigger does not match the installation.")
    caller = native_sources.current_device(installation.owner, public_key=metadata["author_pubkey"])
    occurred = datetime.fromtimestamp(metadata["created_at"], tz=datetime_timezone.utc)
    expires = min(grant.expires_at, occurred + timedelta(minutes=15), now + timedelta(minutes=15))
    if expires <= now or occurred > now + timedelta(minutes=1):
        return None
    existing = AgentInvocation.objects.filter(installation=installation, source_event_id=metadata["event_id"]).first()
    if existing:
        return existing
    admitted = AgentInvocation.objects.filter(installation=installation,
        created_at__gte=now.replace(hour=0, minute=0, second=0, microsecond=0)).count()
    if admitted >= grant.daily_task_limit:
        # The caller holds the owner/install/grant lock: concurrent admissions
        # cannot spend another slot, and no webhook/inference is triggered.
        return None
    invocation, created = AgentInvocation.objects.get_or_create(installation=installation,
        source_event_id=metadata["event_id"], defaults={"grant": grant, "generation": grant.generation,
            "resource": grant.resources[0], "thread_id": metadata["root_event_id"],
            "owner_public_key": caller.public_key, "expires_at": expires})
    if created:
        from .events import ingest_event
        ingest_event(source="native", source_event_id=str(invocation.pk), resource=invocation.resource,
            event_type="agent.mentioned", occurred_at=occurred, payload={"installation_id": str(installation.pk),
                "invocation_id": str(invocation.pk), "source_event_id": invocation.source_event_id}, actor_kind="human")
        runtime.audit("native.invocation.created", grant=grant, invocation_id=str(invocation.pk))
    return invocation


def poll_native_mentions(*, limit=50, now=None):
    now = now or timezone.now()
    try:
        native_sources.require_native()
    except PermissionDenied:
        return 0
    count = 0
    ids = list(AgentInstallation.objects.filter(app_id="openai-dot", is_active=True, state="active")
               .order_by("updated_at", "pk").values_list("pk", flat=True)[:min(limit, 100)])
    for installation_id in ids:
        try:
            with transaction.atomic():
                grant_id = AgentGrant.objects.filter(installation_id=installation_id, revoked_at__isnull=True,
                    expires_at__gt=now).order_by("-created_at").values_list("pk", flat=True).first()
                if grant_id is None:
                    continue
                grant = runtime._grant(grant_id)
                from .unified_authority import assert_unified_authority
                assert_unified_authority(grant, allow_pending=True)
                validate_grant_base(grant, now=now)
                installation = grant.installation
                resource = grant.resources[0]
                if len(grant.resources) != 1:
                    raise AgentPermissionDenied("native_pilot_single_channel")
                from .sources import access_checker_for
                authorize(grant, "events:read", resource, access_checker=access_checker_for(grant), now=now)
                device = native_sources.current_device(installation.owner, public_key=installation.owner_device.public_key)
                if device.pk != installation.owner_device_id:
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
                expires_at = parse_datetime(active.delivery.get("expires_at", "")) if active else None
                if not listening_since or not expires_at or expires_at <= now:
                    active = None
                if active:
                    from .unified_authority import assert_record_authority
                    assert_record_authority(grant, active.delivery)
                for event in events:
                    # Starting/resuming listening does not activate earlier mentions.
                    if not isinstance(event, dict):
                        raise ValidationError("Native event page is invalid.")
                    if active and type(event.get("created_at")) is int and event["created_at"] >= int(listening_since.timestamp()):
                        if admit_metadata(grant, event, now=now):
                            count += 1
                if cursor.get("gap_detected"):
                    new_cursor.update(gap_detected=True, error_code="native_cursor_reset_required")
                installation.native_cursor = new_cursor
                installation.save(update_fields=("native_cursor", "updated_at"))
        except native_sources.NativeCursorReset:
            AgentInstallation.objects.filter(pk=installation_id).update(
                native_cursor={"gap_detected": True, "error_code": "native_cursor_reset_required"})
            # Next poll captures a fresh head; acknowledge missed updates by pause/resume.
            continue
        except AgentPermissionDenied as exc:
            if exc.code.startswith("unified_"):
                # Enrolled bots need fresh OAuth consent and an authenticated
                # connection before listening. Lost consent is not a relay outage.
                continue
            current = AgentInstallation.objects.filter(pk=installation_id).values_list("native_cursor", flat=True).first() or {}
            AgentInstallation.objects.filter(pk=installation_id).update(native_cursor={**current, "error_code": "native_source_unavailable"})
            continue
        except (PermissionDenied, ValidationError, native_sources.SourceUnavailable, ValueError):
            # A failure does not advance the durable source cursor.
            current = AgentInstallation.objects.filter(pk=installation_id).values_list("native_cursor", flat=True).first() or {}
            AgentInstallation.objects.filter(pk=installation_id).update(native_cursor={**current, "error_code": "native_source_unavailable"})
            continue
        finally:
            # Round-robin admission includes failed or expired installations.
            AgentInstallation.objects.filter(pk=installation_id).update(updated_at=now)
    return count


def dispatch_native_replies(*, limit=50, now=None):
    now = now or timezone.now()
    try:
        native_sources.require_native(reply=True)
    except PermissionDenied:
        return 0
    sent = 0
    ids = list(AgentInvocationReply.objects.filter(status__in=("pending", "unknown"))
               .order_by("created_at").values_list("invocation_id", flat=True)[:min(limit, 100)])
    for invocation_id in ids:
        with transaction.atomic():
            grant_id = AgentInvocation.objects.values_list("grant_id", flat=True).get(pk=invocation_id)
            grant = runtime._grant(grant_id)
            invocation = runtime.locked(AgentInvocation.objects.all()).get(pk=invocation_id)
            invocation.installation, invocation.grant = grant.installation, grant
            row = runtime.locked(AgentInvocationReply.objects.all()).get(invocation=invocation)
            row.invocation = invocation
            if row.status not in {"pending", "unknown"}:
                continue
            try:
                from .unified_authority import assert_record_authority
                assert_record_authority(grant, row.receipt)
                validate_invocation(invocation, grant, now=now)
                bound = native_sources.current_device(grant.installation.owner, public_key=grant.installation.owner_device.public_key)
                if bound.pk != grant.installation.owner_device_id:
                    raise AgentPermissionDenied("owner_device_changed")
                native_sources.current_device(grant.installation.owner, public_key=invocation.owner_public_key)
                checker = lambda owner, action, resource: native_sources.live_access(owner, action, resource, owner_public_key=invocation.owner_public_key)
                authorize(grant, "messages:reply", invocation.resource, expected_generation=invocation.generation,
                          access_checker=checker, now=now)
                row.receipt = native_sources.send_reply(row)
                row.status, row.error_code = "sent", ""
                invocation.status = "replied"
                invocation.save(update_fields=("status",))
                sent += 1
            except (AgentPermissionDenied, PermissionDenied):
                if row.status == "unknown":
                    row.status, row.error_code = "stopped_unknown", "authority_changed_after_ambiguous_send"
                else:
                    row.status, row.error_code = "cancelled", "authority_changed"
            except (native_sources.SourceUnavailable, ValidationError):
                # Retry the durable same signed event; never create a replacement.
                row.status, row.error_code = "unknown", "relay_receipt_unavailable"
            row.save(update_fields=("status", "receipt", "error_code", "updated_at"))
    return sent
