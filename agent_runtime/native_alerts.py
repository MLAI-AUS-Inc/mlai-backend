"""Owner-only lapse alerts containing identifiers and counts, never chat text."""

from datetime import timedelta
import logging
from types import SimpleNamespace

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import runtime
from .event_webhooks import lease_live
from .models import AgentAuditEvent, AgentInstallation, AgentInvocation, AgentSubscription
from .scheduler_health import report_failure

logger = logging.getLogger(__name__)


def _notify(installation, grant, *, now):
    owner = installation.owner
    if not owner.is_active or not str(owner.email or "").strip():
        return 0
    watches = list(AgentSubscription.objects.filter(grant__installation=installation,
        event_types=["agent.mentioned"], delivery__mode="webhook").order_by("-updated_at", "-pk"))
    if grant and any(row.grant_id == grant.pk and row.generation == grant.generation
                     and row.is_active and lease_live(row.delivery, now)
                     and not row.delivery.get("held_reason") for row in watches):
        return 0
    held = next((row for row in watches if row.is_active and row.delivery.get("held_reason")), None)
    stopped = next((row for row in watches if not row.is_active), None)
    stop = held.delivery if held else stopped.delivery if stopped else {}
    key = (f"held:{held.pk}:{stop.get('held_at')}" if held else stop.get("stopped_at")
           or (stopped.updated_at.isoformat() if stopped else f"never:{grant.pk if grant else 'none'}"))
    history = AgentAuditEvent.objects.filter(grant=grant, metadata__installation_id=str(installation.pk),
                                            metadata__lapse_key=key)
    if history.filter(event_type="native.alert.sent").exists():
        return 0
    if history.filter(event_type="native.alert.failed", created_at__gt=now - timedelta(minutes=5)).exists():
        return 0
    pending = AgentInvocation.objects.filter(installation=installation, status="pending", expires_at__gt=now).count()
    reason = "needs_reconnect_or_renew" if not grant else "not_listening"
    from .native_installations import setup_prompt
    text = (f"Your personal agent is not currently listening in MLAI Chat.\n"
            f"Waiting messages: {pending}.\nReason: {stop.get('held_reason') or stop.get('stop_reason') or reason}.\n"
            f"Stopped at: {stop.get('stopped_at') or stop.get('held_at') or 'not recorded'}.\n\n"
            f"Open your Dot and send this once:\n{setup_prompt()}\n\n"
            "Queued messages expire after their queue lifetime. Reconnect or renew the MLAI connection if prompted.")
    from integrations.services.notification_adapters import _send_email
    try:
        sent, _identifier, _result = _send_email(
            SimpleNamespace(route_id=owner.email, user_id=owner.pk),
            subject="Your MLAI agent needs attention", text=text, html_body=None,
            idempotency_key=f"maek-lapse:{installation.pk}:{key}")
    except Exception:
        # Provider exceptions may contain recipient addresses and request bodies.
        sent = False
    runtime.audit("native.alert.sent" if sent else "native.alert.failed", grant=grant,
                  installation_id=str(installation.pk), lapse_key=key,
                  reason=reason, pending_count=pending)
    return int(bool(sent))


def check_native_listening(*, now=None):
    """Send one acknowledged alert per lapse, under the installation fence."""
    if not (getattr(settings, "AGENT_RUNTIME_ENABLED", False)
            and getattr(settings, "AGENT_RUNTIME_NATIVE_LAPSE_ALERTS", False)):
        return 0
    now, sent = now or timezone.now(), 0
    identifiers = AgentInstallation.objects.filter(app_id="openai-dot", is_active=True,
        state="active").values_list("pk", flat=True)
    for identifier in identifiers.iterator(chunk_size=100):
        try:
            with transaction.atomic():
                # Match runtime's user -> installation -> grant lock order. Network
                # delivery is serialized with other workers to avoid duplicate mail.
                owner_id = AgentInstallation.objects.values_list("owner_id", flat=True).get(pk=identifier)
                from django.contrib.auth import get_user_model
                owner = runtime.locked(get_user_model().objects.all()).get(pk=owner_id)
                installation = runtime.locked(AgentInstallation.objects.all()).get(pk=identifier)
                if (installation.owner_id != owner.pk or not installation.is_active or installation.state != "active"):
                    continue
                installation.owner = owner
                grant = runtime.locked(installation.grants.filter(revoked_at__isnull=True, expires_at__gt=now)
                                       .order_by("-created_at")).first()
                sent += _notify(installation, grant, now=now)
        except Exception as exc:
            report_failure(logger, "native_lapse_alert", exc)
    return sent
