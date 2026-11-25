"""All polling and delivery use the same durable room identity as webhook intake."""
import logging
import time
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from integrations.models import ManagedChannelScope, ManagedDelivery, ManagedEntity, ManagedReceipt, ManagedReference, ManagedRoom

from . import luma
from .config import installations, scope_config
from .registry import eligible_room, observe, scope_for

logger = logging.getLogger(__name__)


def adapter(provider):
    if provider == "luma":
        return luma
    from . import projects
    return projects


def _retry(attempts):
    return timezone.now() + timedelta(seconds=min(3600, 15 * 2 ** min(attempts, 8)))


def _tombstone(ref):
    if ref and ref.snapshot:
        return {**ref.snapshot, "status": "removed", "url": ""}
    return None


def scan(config, *, dry_run=False, force=False):
    provider = config["provider"]
    service = adapter(provider)
    if dry_run:
        rows = service.inventory(provider, config)
        references = {ref.external_id: ref for ref in ManagedReference.objects.select_related("entity").filter(
            scope__community=config["community"], scope__provider=provider, scope__scope=config["scope"])}
        proposed = []
        for row in rows:
            external_id = row["external_id"]
            reference = references.get(external_id)
            room = ManagedRoom.objects.filter(entity=reference.entity).first() if reference else None
            policy = None if row.get("hold_reason") else eligible_room(config, row)
            if row.get("hold_reason"):
                action = row["hold_reason"]
            elif not policy:
                action = "hold_audience"
            elif room:
                action = "refresh" if row != reference.snapshot else "unchanged"
            elif policy["channel_id"]:
                action = "adopt"
            elif row.get("alias"):
                action = "resolve_verified_mirror"
            elif row["status"] in {"archived", "removed", "closed", "canceled", "completed", "ended"} and not config.get("backfill_historical"):
                action = "hold_historical"
            else:
                action = "create"
            proposed.append({"external_id": external_id, "action": action,
                             "channel_id": str(room.channel_id) if room else str((policy or {}).get("channel_id") or "")})
        seen = {row["external_id"] for row in rows}
        proposed.extend({"external_id": key, "action": "verify_absence"} for key in sorted(references.keys() - seen))
        return {"listings": len(rows), "bindings": proposed}
    scope = scope_for(config)
    with transaction.atomic():
        scope = ManagedChannelScope.objects.select_for_update().get(pk=scope.pk)
        if not force and scope.retry_at > timezone.now():
            return {"deferred": True}
        # Only a complete, well-formed, authorized inventory may trigger missing-ID checks.
        rows = service.inventory(provider, config)
        seen = {}
        for row in rows:
            key = row["external_id"]
            if not isinstance(key, str) or not key or len(key) > 255 or key in seen:
                raise ValueError("Incomplete or ambiguous source inventory")
            seen[key] = row
        for row in seen.values():
            if row.get("hold_reason"):
                _queue_check(scope, row["external_id"], "identity", row["hold_reason"])
            else:
                try:
                    observe(scope, config, row)
                except (ValueError, IntegrityError):
                    # observe has its own savepoint. Preserve unrelated rooms
                    # and retry just the conflicting entity from a fresh read.
                    _queue_check(scope, row["external_id"], "identity", "binding_conflict")
        for ref in ManagedReference.objects.filter(scope=scope).exclude(external_id__in=seen):
            if not ref.snapshot or ref.snapshot.get("status") == "removed":
                continue
            # Per-ID lookups are independent durable work. An inconclusive private
            # lookup cannot discard a successful scan or the prior confirmed state.
            _queue_check(scope, ref.external_id, "missing")
        scope.scanned_at = timezone.now()
        scope.retry_at = scope.scanned_at + timedelta(minutes=15)
        scope.error = ""
        scope.save(update_fields=["scanned_at", "retry_at", "error"])
        return {"listings": len(seen)}


def _queue_check(scope, external_id, action, reason=""):
    import hashlib
    key = hashlib.sha256(external_id.encode()).hexdigest()
    receipt, created = ManagedReceipt.objects.get_or_create(
        scope=scope, delivery_id=f"local:{action}:{key}",
        defaults={"action": action, "external_id": external_id, "error": reason},
    )
    if not created and receipt.completed_at:
        receipt.completed_at = None
        receipt.due_at = timezone.now()
        receipt.save(update_fields=["completed_at", "due_at"])


def receive(config, delivery_id, external_id, action):
    scope = scope_for(config)
    receipt, created = ManagedReceipt.objects.get_or_create(scope=scope, delivery_id=delivery_id,
        defaults={"external_id": external_id, "action": action})
    if receipt.external_id != external_id or receipt.action != action:
        raise ValueError("Conflicting provider delivery ID")
    return created


@transaction.atomic
def refresh(pk, config):
    receipt = ManagedReceipt.objects.select_for_update().get(pk=pk)
    if receipt.completed_at or receipt.due_at > timezone.now():
        return
    scope = ManagedChannelScope.objects.select_for_update().get(pk=receipt.scope_id)
    ref = ManagedReference.objects.filter(scope=scope, external_id=receipt.external_id).first()
    if scope.provider == "luma":
        data = luma.get_snapshot("luma", config, receipt.external_id, ref.snapshot if ref else None)
        if receipt.action == "canceled" and ref and data is None:
            data = ref.snapshot
    else:
        from . import projects
        try:
            data = projects.get_snapshot(scope.provider, config, receipt.external_id)
        except projects.ProjectAccessRevoked:
            data = ({"external_id": receipt.external_id, "status": "restricted", "title": "", "url": "",
                     "visibility": "unknown", "public_authorized": False}
                    if ref and ref.snapshot else None)
        except projects.ProjectNotFound:
            if receipt.action != "removed" or not ref:
                raise
            data = None
    if data is None:
        data = _tombstone(ref)
    if data is not None:
        if data["external_id"] != receipt.external_id:
            raise ValueError("Provider returned a different identifier")
        observe(scope, config, data, canceled=receipt.action == "canceled")
    receipt.completed_at = timezone.now()
    receipt.error = ""
    receipt.save(update_fields=["completed_at", "error"])


def _payload(entity):
    keys = ("title", "status", "url", "start_at", "end_at", "timezone")
    result = {"revision": entity.revision, "section": entity.section,
              **{key: entity.snapshot.get(key) or "" for key in keys}}
    for key in ("links", "latest_project_update"):
        if entity.snapshot.get(key):
            result[key] = entity.snapshot[key]
    return result


def deliver(pk, config):
    with transaction.atomic():
        work = ManagedDelivery.objects.select_for_update().get(pk=pk)
        entity = ManagedEntity.objects.get(pk=work.entity_id)
        if work.applied_revision >= entity.revision or work.due_at > timezone.now():
            return
        if work.target != "chat" and not ManagedDelivery.objects.filter(
                entity=entity, target="chat", applied_revision=entity.revision).exists():
            work.due_at = timezone.now() + timedelta(seconds=45)
            work.save(update_fields=["due_at"])
            return
        work.attempts += 1
        work.due_at = timezone.now() + timedelta(minutes=2)
        work.save(update_fields=["attempts", "due_at"])
    room = ManagedRoom.objects.get(entity=entity)
    try:
        if config.get("write_chat") is not True:
            raise ValueError("chat_destination_paused")
        if work.target == "chat":
            from .relay_client import apply_room
            payload = _payload(entity)
            if room.section_override:
                payload["section"] = room.section_override
            apply_room(config, room, payload)
        elif work.target == "backlink":
            if entity.snapshot["status"] not in {"restricted", "removed", "archived"}:
                from .projects import sync_backlink
                ref = ManagedReference.objects.select_related("scope").get(entity=entity, scope__provider="linear")
                link_config = scope_config(ref.scope)
                if not link_config or link_config.get("write_backlinks") is not True:
                    raise ValueError("backlink_destination_paused")
                source = ref.snapshot
                policy = eligible_room(link_config, source) if source.get("status") not in {
                    None, "restricted", "removed", "archived"} else None
                if not policy or (room.visibility == "private" and (
                        policy["visibility"] != "private" or not set(policy["members"]).issubset(room.members))):
                    raise ValueError("backlink_audience_unconfirmed")
                sync_backlink("linear", link_config, ref.external_id, str(room.channel_id))
        else:
            from .slack import sync
            sync(config, room, entity.snapshot)
    except Exception as exc:
        from .slack import SlackHold
        bounded_reasons = {"chat_destination_paused", "backlink_destination_paused", "backlink_audience_unconfirmed"}
        reason = str(exc) if isinstance(exc, SlackHold) or str(exc) in bounded_reasons else type(exc).__name__
        ManagedDelivery.objects.filter(pk=pk, attempts=work.attempts).update(
            error=reason[:120], due_at=_retry(work.attempts))
        return
    ManagedDelivery.objects.filter(pk=pk, attempts=work.attempts).update(
        applied_revision=entity.revision, due_at=timezone.now(), error="", attempts=0)


def tick(*, only=None, force=False):
    configs = installations()
    if only:
        configs = {only: configs[only]}
    if not configs:
        return {"status": "disabled"}
    # Each invocation bounds the number of independent network operations.
    # Enabled scopes alternate using their durable due times after a failure.
    started = time.monotonic()
    for config in configs.values():
        scope = scope_for(config)
        if not force and scope.retry_at > timezone.now():
            continue
        try:
            scan(config, force=force)
        except Exception as exc:
            ManagedChannelScope.objects.filter(pk=scope.pk).update(
                retry_at=_retry(2), error=type(exc).__name__)
            logger.warning("managed_channel_scan_deferred scope_id=%s error=%s", scope.pk, type(exc).__name__)
        if time.monotonic() - started > 25:
            break
    scopes = [scope_for(config) for config in configs.values()]
    # Clock transitions use stored UTC instants. The same scope lock as ingestion
    # ensures a late timer cannot apply an obsolete start or end time.
    for scope in (s for s in scopes if s.provider == "luma"):
        with transaction.atomic():
            scope = ManagedChannelScope.objects.select_for_update().get(pk=scope.pk)
            for ref in ManagedReference.objects.filter(scope=scope, canceled=False, snapshot__status__in=["live","scheduled"]):
                state = luma.status_now(ref.snapshot)
                if state != ref.snapshot["status"]:
                    observe(scope, scope_config(scope), {**ref.snapshot, "status": state})
    receipts = ManagedReceipt.objects.filter(scope__in=scopes, completed_at__isnull=True,
                                             due_at__lte=timezone.now()).order_by("due_at", "pk")
    for item in receipts.select_related("scope")[:8]:
        try:
            refresh(item.pk, scope_config(item.scope))
        except Exception as exc:
            ManagedReceipt.objects.filter(pk=item.pk, completed_at__isnull=True).update(
                due_at=_retry(item.attempts + 1), attempts=F("attempts") + 1, error=type(exc).__name__)
        if time.monotonic() - started > 45:
            break
    work = ManagedDelivery.objects.filter(entity__authority__in=scopes, due_at__lte=timezone.now(),
        applied_revision__lt=F("entity__revision")).select_related("entity__authority").order_by("due_at", "pk")
    for item in work[:12]:
        deliver(item.pk, scope_config(item.entity.authority))
        if time.monotonic() - started > 55:
            break
    return {"status": "ok"}
