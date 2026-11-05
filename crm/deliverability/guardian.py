"""Rate-based campaign protection. Provider callbacks only record; this runner acts."""

import hashlib
import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from crm import listmonk
from crm.blasts import BlastError
from crm.models import Blast, CrmAuditEvent, DeliverabilityIncident, EmailEvent, SendingStream

from . import policy
from .streams import is_owner, protected_domain, stream_configured

logger = logging.getLogger(__name__)
LIVE = (Blast.Status.QUEUED, Blast.Status.SENDING)


@dataclass(frozen=True)
class Metrics:
    attempted: int = 0
    delivered: int = 0
    hard_bounces: int = 0
    complaints: int = 0
    policy_blocks: int = 0


@dataclass(frozen=True)
class Signal:
    key: str
    severity: str
    value: float
    threshold: float
    sample: int
    action: str


def evaluate(metrics, *, canary=False, min_sample=None):
    """Thresholds are inclusive; one address-level event never stops a blast."""
    p = policy.value
    signals = []
    minimum = min_sample if canary and type(min_sample) is int and min_sample > 0 else p("CRM_CANARY_MIN_SAMPLE" if canary else "CRM_GUARD_HARD_BOUNCE_MIN_SAMPLE")
    absolute = canary and metrics.hard_bounces >= p('CRM_CANARY_HARD_BOUNCE_COUNT')
    if metrics.attempted and (metrics.attempted >= minimum or absolute):
        rate = metrics.hard_bounces / metrics.attempted
        if rate >= p("CRM_GUARD_HARD_BOUNCE_RED") or absolute:
            signals.append(Signal("hard_bounce_rate", "red", rate, p("CRM_GUARD_HARD_BOUNCE_RED"), metrics.attempted, "pause_blast"))
        elif rate >= p("CRM_GUARD_HARD_BOUNCE_AMBER"):
            signals.append(Signal("hard_bounce_rate", "amber", rate, p("CRM_GUARD_HARD_BOUNCE_AMBER"), metrics.attempted, "slow_stream"))
    if metrics.delivered and metrics.complaints >= p("CRM_GUARD_COMPLAINT_MIN_COUNT"):
        rate = metrics.complaints / metrics.delivered
        red = ((metrics.complaints >= p("CRM_GUARD_COMPLAINT_RED_MIN_COUNT") and rate >= p("CRM_GUARD_COMPLAINT_RED"))
               or (metrics.delivered >= p("CRM_GUARD_COMPLAINT_MIN_SAMPLE") and rate >= p("CRM_GUARD_COMPLAINT_HIGH")))
        if red:
            threshold = p("CRM_GUARD_COMPLAINT_RED") if metrics.complaints >= p("CRM_GUARD_COMPLAINT_RED_MIN_COUNT") else p("CRM_GUARD_COMPLAINT_HIGH")
            signals.append(Signal("complaint_rate", "red", rate, threshold, metrics.delivered, "stop_stream"))
        elif metrics.complaints >= p("CRM_GUARD_COMPLAINT_MIN_COUNT") and rate >= p("CRM_GUARD_COMPLAINT_AMBER"):
            signals.append(Signal("complaint_rate", "amber", rate, p("CRM_GUARD_COMPLAINT_AMBER"), metrics.delivered, "slow_stream"))
    if metrics.policy_blocks >= p("CRM_GUARD_POLICY_BLOCK_MIN_COUNT"):
        signals.append(Signal("policy_block", "red", metrics.policy_blocks, p("CRM_GUARD_POLICY_BLOCK_MIN_COUNT"), metrics.policy_blocks, "stop_stream"))
    return signals


def policy_block(event):
    """Only documented policy/authentication rejections count, never generic failures."""
    provider, code, diagnostic = event.recipient_provider, event.enhanced_code, event.diagnostic.lower()
    if provider == "google":
        return code in ("5.7.26", "5.7.27") or (code == "5.7.1" and any(word in diagnostic for word in ("unsolicited", "spam")))
    if provider == "microsoft":
        return code == "5.7.515" or (code == "5.7.1" and any(word in diagnostic for word in ("s3140", "s3150")))
    if provider == "yahoo":
        return code == "5.7.9" or "tss09" in diagnostic
    if provider == "apple":
        return "hm07" in diagnostic or "hm08" in diagnostic
    return False


def blast_metrics(blast, *, now, wave=None):
    """Deduplicate by recipient message, including complaints after a delivery status changed."""
    rows = blast.deliveries.filter(channel="email").exclude(address__iendswith="@resend.dev")
    if wave:
        rows = rows.filter(wave=wave)
    counts = rows.aggregate(
        attempted=Count('pk', filter=Q(status__in=("submitting", "sent", "delivered", "read", "bounced", "undelivered", "unknown")) | ~Q(provider_id="")),
        delivered=Count('pk', filter=Q(delivered_at__isnull=False) | Q(status__in=("delivered", "read")) | Q(error_code="resend_complained")),
        hard=Count('pk', filter=Q(bounce_kind='hard', status='bounced')),
        complaints=Count('pk', filter=Q(error_code='resend_complained')))
    events = EmailEvent.objects.filter(blast=blast).exclude(recipient__iendswith="@resend.dev")
    if wave:
        events = events.filter(wave=wave)
    # Legacy rows may have provider feedback without populated bounce_kind yet.
    receipts = events.aggregate(
        hard=Count('provider_message_id', distinct=True, filter=Q(event_type='email.bounced') & ~Q(bounce_type='Transient')),
        complaints=Count('provider_message_id', distinct=True, filter=Q(event_type='email.complained')))
    hard = max(counts['hard'], receipts['hard'])
    complaints = max(counts['complaints'], receipts['complaints'])
    recent = events.filter(received_at__gte=now - timedelta(hours=policy.value("CRM_GUARD_POLICY_WINDOW_HOURS")), event_type__in=("email.bounced", "email.failed"))
    blocked_ids = {event.provider_message_id for event in recent if policy_block(event)}
    return Metrics(counts['attempted'], max(counts['delivered'], complaints), hard, complaints, len(blocked_ids))


def fingerprint(stream_id, blast_id, signal, wave_id=None):
    """Stable across repeated detections; severity escalation updates the same incident."""
    scope = f"{stream_id}:{blast_id}:{signal}" + (f':{wave_id}' if wave_id else '')
    return hashlib.sha256(scope.encode()).hexdigest()


def _audit(incident, operation, *, actor=None, **extra):
    CrmAuditEvent.objects.create(actor=actor, action="edit", target_type="deliverability_incident",
        target_id=str(incident.pk), detail={"operation": operation, "signal": incident.signal,
            "severity": incident.severity, "action": incident.action, "sample": incident.sample,
            "value": incident.value, "threshold": incident.threshold, **extra})


def detect(blast, signal, *, now, wave=None):
    """Durably pause admission before attempting the external campaign pause."""
    dry_run = policy.value("CRM_GUARDIAN_DRY_RUN")
    with transaction.atomic():
        stream = SendingStream.objects.select_for_update().get(pk=blast.stream_id)
        if stream.transactional:
            return None
        prior = DeliverabilityIncident.objects.filter(
            fingerprint=fingerprint(stream.pk, blast.pk, signal.key, wave.pk if wave else None)
        ).order_by("-opened_at", "-pk").first()
        if (prior and prior.state == "acknowledged"
                and signal.value * signal.sample <= prior.value * prior.sample):
            # Owner recovery acknowledges this evidence, not future negative feedback.
            return None
        incident, created = DeliverabilityIncident.objects.get_or_create(
            fingerprint=fingerprint(stream.pk, blast.pk, signal.key, wave.pk if wave else None), state="open", defaults={
                "stream": stream, "blast": blast, "wave": wave, "signal": signal.key, "severity": signal.severity,
                "value": signal.value, "threshold": signal.threshold, "sample": signal.sample,
                "action": signal.action, "opened_at": now, "last_detected_at": now,
            })
        escalated = incident.severity == "amber" and signal.severity == "red"
        if created or escalated or incident.severity == signal.severity:
            incident.severity, incident.action = signal.severity, signal.action
            incident.value, incident.threshold, incident.sample = signal.value, signal.threshold, signal.sample
        incident.last_detected_at = now
        if escalated:
            incident.action_applied_at, incident.last_alerted_at = None, None
        incident.save()
        if not dry_run and (created or escalated or incident.action_applied_at is None):
            state_changes = True
            if incident.action == "stop_stream":
                stream.state = "stopped"
                targets = Blast.objects.filter(stream=stream, status__in=LIVE)
            elif incident.action == "pause_blast":
                # A hold permits existing healthy campaigns to finish, but no new starts.
                if stream.state != "stopped":
                    stream.state = "held"
                else:
                    state_changes = False
                targets = Blast.objects.filter(pk=blast.pk, status__in=LIVE)
            else:
                if stream.state not in ("held", "stopped"):
                    stream.state = "slowed"
                else:
                    state_changes = False
                targets = Blast.objects.none()
            if state_changes:
                stream.state_reason, stream.state_changed_at = incident.signal, now
                stream.state_detail = {"incident_id": str(incident.pk)}
                stream.save(update_fields=["state", "state_reason", "state_changed_at", "state_detail", "updated_at"])
            # Match start_campaign's stream → blast lock order.
            for target in targets.select_for_update().order_by("pk"):
                # Preserve a staff pause as system protection; acknowledging must not restart it.
                if target.pause_state == "staff":
                    continue
                target.pause_state, target.pause_reason, target.paused_at = "system", incident.signal, now
                target.save(update_fields=["pause_state", "pause_reason", "paused_at", "updated_at"])
        if created or escalated:
            _audit(incident, "detected", dry_run=dry_run)
    return incident


def enforce(incident, *, now):
    """Retry provider pauses until successful; database admission remains paused on outages."""
    if policy.value("CRM_GUARDIAN_DRY_RUN") or incident.action_applied_at:
        return
    targets = Blast.objects.filter(stream=incident.stream, pause_state__in=("system", "staff"), status__in=LIVE)
    if incident.action == "pause_blast":
        targets = targets.filter(pk=incident.blast_id)
    elif incident.action != "stop_stream":
        targets = targets.none()
    for target in targets.order_by("pk"):
        # Serialize an owner resume against a delayed guardian provider request.
        with transaction.atomic():
            SendingStream.objects.select_for_update().get(pk=incident.stream_id)
            current = DeliverabilityIncident.objects.get(pk=incident.pk)
            if current.state != "open":
                return
            target = Blast.objects.select_for_update().get(pk=target.pk)
            if target.pause_state:
                wave_ids = list(target.waves.filter(status__in=('loading', 'running', 'paused'),
                    listmonk_campaign_id__isnull=False).values_list('listmonk_campaign_id', flat=True))
                campaign_ids = wave_ids or ([target.listmonk_campaign_id] if target.listmonk_campaign_id else [])
                for campaign_id in campaign_ids:
                    campaign = listmonk.get_campaign(campaign_id)
                    if campaign.get("status") == listmonk.RUNNING:
                        listmonk.set_campaign_status(campaign_id, "paused")
    with transaction.atomic():
        current = DeliverabilityIncident.objects.select_for_update().get(pk=incident.pk)
        if current.state == "open" and current.action_applied_at is None:
            current.action_applied_at = now
            current.save(update_fields=["action_applied_at", "updated_at"])
            _audit(current, "applied")


def acknowledge(incident_id, *, actor, resolution, note="", now=None):
    """An owner explicitly decides recovery; other open red incidents retain their stop."""
    if not is_owner(actor):
        raise BlastError("owner_required", 403)
    if resolution not in ("resume_slow", "resume", "stop_blast", "keep_stopped") or len(note) > 2000:
        raise BlastError("invalid_resolution")
    now = now or timezone.now()
    stream_id = DeliverabilityIncident.objects.values_list("stream_id", flat=True).get(pk=incident_id)
    with transaction.atomic():
        stream = SendingStream.objects.select_for_update().get(pk=stream_id)
        incident = DeliverabilityIncident.objects.select_for_update().get(pk=incident_id)
        if incident.state == "acknowledged" and incident.resolution == resolution:
            return incident
        deferred_resume = (incident.state == "acknowledged"
                           and incident.resolution in ("keep_stopped", "stop_blast")
                           and resolution in ("resume", "resume_slow"))
        if incident.state != "open" and not deferred_resume:
            raise BlastError("incident_not_open", 409)
        incident.state, incident.resolution, incident.notes = "acknowledged", resolution, note
        incident.acknowledged_by, incident.acknowledged_at = actor, now
        incident.save()
        if resolution in ("resume", "resume_slow"):
            from crm.sending_domains import is_verified
            if not stream_configured(stream) or stream.transactional:
                raise BlastError("stream_not_configured", 409)
            if not settings.CRM_SENDING_ENABLED or not stream.mailbox.enabled:
                raise BlastError("channel_disabled", 409)
            if not is_verified(stream.domain):
                raise BlastError("sending_system_unverified", 409)
            if stream.incidents.filter(state="open", severity="red").exists():
                raise BlastError("other_incidents_open", 409)
            stream.ramp_stage = max(0, stream.ramp_stage - policy.value("CRM_GUARD_RESUME_DEMOTION"))
            stream.ramp_updated_at = now
            stream.state = "slowed" if resolution == "resume_slow" else "warming"
            stream.state_reason, stream.state_changed_at = "owner_resumed", now
            stream.state_detail = {"slow_until": (now + timedelta(hours=policy.value("CRM_GUARD_RESUME_SLOW_HOURS"))).isoformat()} if resolution == "resume_slow" else {}
            stream.save()
            for blast in Blast.objects.select_for_update().filter(stream=stream, pause_state="system", status__in=LIVE).order_by("pk"):
                if (not blast.mailbox_id or not blast.mailbox.enabled
                        or protected_domain(blast.mailbox.email.rsplit("@", 1)[-1])
                        or blast.mailbox.domain != stream.domain):
                    raise BlastError("mailbox_stream_mismatch", 409)
                if blast.listmonk_campaign_id or blast.waves.filter(status='paused').exists():
                    from crm.blast_email import with_email_stats
                    # Commit the owner's decision before the dispatcher resumes any provider.
                    # A later row validation or DB failure cannot roll back an external send.
                    blast.stats = with_email_stats(blast.stats, resume_pending=True)
                blast.pause_state, blast.pause_reason, blast.paused_at, blast.paused_by = "", "", None, None
                blast.save(update_fields=["pause_state", "pause_reason", "paused_at", "paused_by", "stats", "updated_at"])
        elif resolution == "stop_blast" and incident.blast_id:
            from crm.blasts import cancel_blast
            if Blast.objects.filter(pk=incident.blast_id, status__in=(Blast.Status.DRAFT, *LIVE)).exists():
                cancel_blast(incident.blast_id, actor=actor, now=now)
        _audit(incident, "acknowledged", actor=actor, resolution=resolution)
    return incident


def admission_ready():
    """A configured guardian must have completed a recent pass before new starts."""
    return bool(cache.get("crm:deliverability:healthy"))


def run_deliverability_guard(*, now=None):
    """Self-throttled and never raises into the shared scheduler."""
    if not policy.value("CRM_GUARDIAN_ENABLED"):
        return {"status": "disabled"}
    if policy.value("CRM_GUARDIAN_DRY_RUN") and settings.APP_ENV == "production":
        return {"status": "deferred", "reason": "dry_run_forbidden_in_production"}
    now = now or timezone.now()
    try:
        if not cache.add("crm:deliverability:guard", 1, timeout=policy.value("CRM_GUARD_INTERVAL_SECONDS")):
            return {"status": "throttled"}
        # Retry pending external effects even when their original sample has aged out.
        for incident in DeliverabilityIncident.objects.filter(state="open", action_applied_at__isnull=True).order_by("opened_at"):
            enforce(incident, now=now)
        blasts = Blast.objects.filter(stream__transactional=False).filter(
            Q(status__in=LIVE) | Q(sent_at__gte=now - timedelta(days=policy.value("CRM_GUARD_LOOKBACK_DAYS")))
        ).order_by("updated_at", "pk")
        detected = 0
        for blast in blasts:
            scopes = [None, *blast.waves.filter(started_at__isnull=False).filter(
                Q(status__in=('running', 'paused')) | Q(finished_at__gte=now - timedelta(days=1)))]
            for wave in scopes:
                canary = bool(wave and wave.kind == 'canary' and not wave.stats.get('legacy_adopted'))
                for signal in evaluate(blast_metrics(blast, now=now, wave=wave), canary=canary,
                        min_sample=wave.stats.get('canary_min_sample') if wave else None):
                    incident = detect(blast, signal, now=now, wave=wave)
                    if incident:
                        enforce(incident, now=now)
                        detected += 1
        from .alerts import alert_open_incidents
        alert_open_incidents(now=now)
        cache.set("crm:deliverability:healthy", True, timeout=policy.value("CRM_GUARD_HEARTBEAT_SECONDS"))
        return {"status": "completed", "signals": detected}
    except Exception as exc:
        logger.error("CRM deliverability guard deferred: %s", type(exc).__name__)
        try:
            cache.delete("crm:deliverability:healthy")
            from .alerts import alert_open_incidents
            alert_open_incidents(now=now)
        except Exception as alert_error:
            logger.error("CRM deliverability alert deferred: %s", type(alert_error).__name__)
        return {"status": "deferred", "reason": "guard_unavailable"}
