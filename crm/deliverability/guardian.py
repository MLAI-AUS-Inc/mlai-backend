"""Rate-based campaign protection. Provider callbacks only record; this runner acts."""

import hashlib
import logging
from dataclasses import dataclass, field, replace
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

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
    unsubscribed: int = 0
    deferred: int = 0
    rate_limited: int = 0
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Signal:
    key: str
    severity: str
    value: float
    threshold: float
    sample: int
    action: str
    evidence_key: str = ""
    scope: str = ""
    provider: str = ""


def evaluate(metrics, *, canary=False, min_sample=None, stream=None):
    """Thresholds are inclusive; one address-level event never stops a blast."""
    p = lambda key: policy.value(key, stream)
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
    from .scopes import blast_metrics as measure
    return measure(blast, now=now, wave=wave)


def incident_fingerprint(stream, blast, signal, wave=None):
    key = signal.key + (':' + signal.scope if signal.scope else '') + (':' + signal.provider if signal.provider else '')
    return fingerprint(stream.pk, blast.pk if blast else None, key, wave.pk if wave else None)


def evidence_acknowledged(prior, signal):
    """An owner accepts exactly these failures; a new failure always reopens protection."""
    if not prior or prior.state not in ('acknowledged', 'resolved'):
        return False
    audit = CrmAuditEvent.objects.filter(target_type='deliverability_incident', target_id=str(prior.pk),
        detail__operation='detected').order_by('-created_at', '-pk').first()
    key = (audit.detail if audit else {}).get('evidence_key', '')
    return bool(signal.evidence_key and key == signal.evidence_key) or (
        not signal.evidence_key and signal.value * signal.sample <= prior.value * prior.sample)


def amber_quiet_since(incident, *, now, signal=None):
    """A completed cohort's unchanged amber evidence may cool without another send."""
    if (not incident or incident.severity != 'amber' or not incident.blast_id or incident.recipient_provider
            or incident.signal not in ('hard_bounce_rate', 'complaint_rate', 'unsubscribe_rate', 'deferral_rate')):
        return None
    audit = CrmAuditEvent.objects.filter(target_type='deliverability_incident', target_id=str(incident.pk),
        detail__operation='detected').order_by('-created_at', '-pk').first()
    if not audit or not audit.detail.get('evidence_key'):
        return None
    if signal and signal.evidence_key != audit.detail['evidence_key']:
        return None
    observed = parse_datetime(audit.detail.get('observed_at', ''))
    if not observed or now < observed + timedelta(hours=policy.value('CRM_GUARD_SLOW_CLEAN_HOURS')):
        return None
    rows = incident.blast.deliveries.filter(channel='email')
    if incident.wave_id:
        rows = rows.filter(wave_id=incident.wave_id)
    if rows.filter(status__in=('submitting', 'sent', 'unknown'), delivered_at__isnull=True).exists():
        return None
    return observed


def signal_pending(stream, blast, signal, *, wave=None, now=None):
    """Admission shares the guardian's acknowledgement boundary, without applying effects inline."""
    prior = DeliverabilityIncident.objects.filter(fingerprint=incident_fingerprint(stream, blast, signal, wave)).order_by('-opened_at', '-pk').first()
    return not (evidence_acknowledged(prior, signal) or (now and amber_quiet_since(prior, now=now, signal=signal)))


def fingerprint(stream_id, blast_id, signal, wave_id=None):
    """Stable across repeated detections; severity escalation updates the same incident."""
    scope = f"{stream_id}:{blast_id}:{signal}" + (f':{wave_id}' if wave_id else '')
    return hashlib.sha256(scope.encode()).hexdigest()


def _audit(incident, operation, *, actor=None, **extra):
    CrmAuditEvent.objects.create(actor=actor, action="edit", target_type="deliverability_incident",
        target_id=str(incident.pk), detail={"operation": operation, "signal": incident.signal,
            "severity": incident.severity, "action": incident.action, "sample": incident.sample,
            "value": incident.value, "threshold": incident.threshold, **extra})


def detect(blast, signal, *, now, wave=None, stream=None):
    """Durably pause admission before attempting the external campaign pause."""
    dry_run = policy.value("CRM_GUARDIAN_DRY_RUN")
    with transaction.atomic():
        stream = SendingStream.objects.select_for_update().get(pk=stream.pk if stream else blast.stream_id)
        if stream.transactional:
            signal = replace(signal, action="alert_only")
        prior = DeliverabilityIncident.objects.filter(
            fingerprint=incident_fingerprint(stream, blast, signal, wave)
        ).order_by("-opened_at", "-pk").first()
        if evidence_acknowledged(prior, signal):
            # Owner recovery acknowledges this evidence, not future negative feedback.
            return None
        if signal.severity == 'amber' and amber_quiet_since(prior, now=now, signal=signal):
            return None
        incident, created = DeliverabilityIncident.objects.get_or_create(
            fingerprint=incident_fingerprint(stream, blast, signal, wave), state="open", defaults={
                "stream": stream, "blast": blast, "wave": wave, "recipient_provider": signal.provider, "signal": signal.key, "severity": signal.severity,
                "value": signal.value, "threshold": signal.threshold, "sample": signal.sample,
                "action": signal.action, "opened_at": now, "last_detected_at": now,
            })
        previous_evidence = CrmAuditEvent.objects.filter(target_type='deliverability_incident', target_id=str(incident.pk),
            detail__operation='detected').order_by('-created_at', '-pk').first() if not created and signal.evidence_key else None
        changed = bool(previous_evidence and previous_evidence.detail.get('evidence_key') != signal.evidence_key)
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
            elif incident.action == 'hold_stream':
                state_changes = stream.state != 'stopped'
                if state_changes:
                    stream.state = 'held'
                targets = Blast.objects.none()
            elif incident.action == 'slow_stream':
                state_changes = stream.state not in ('held', 'stopped')
                if state_changes:
                    stream.state = 'slowed'
                targets = Blast.objects.none()
            else:
                state_changes = False
                targets = Blast.objects.none()
            if state_changes:
                stream.state_reason, stream.state_changed_at = incident.signal, now
                stream.state_detail = {**(stream.state_detail or {}), "incident_id": str(incident.pk)}
                if incident.signal == 'postmaster_spam_rate':
                    if incident.severity == 'red' and stream.state_detail.get('demoted_incident') != str(incident.pk):
                        stream.ramp_stage = max(0, stream.ramp_stage - policy.value('CRM_GUARD_RESUME_DEMOTION'))
                        stream.ramp_updated_at = now
                        stream.state_detail['demoted_incident'] = str(incident.pk)
                    elif incident.severity == 'amber':
                        stream.state_detail['slow_until'] = (now + timedelta(hours=policy.value('CRM_GUARD_POSTMASTER_SLOW_HOURS'))).isoformat()
                        stream.state_detail['postmaster_no_promotion_until'] = stream.state_detail['slow_until']
                stream.save(update_fields=["state", "state_reason", "state_changed_at", "state_detail", "ramp_stage", "ramp_updated_at", "updated_at"])
            # Match start_campaign's stream → blast lock order.
            for target in targets.select_for_update().order_by("pk"):
                # Preserve a staff pause as system protection; acknowledging must not restart it.
                if target.pause_state == "staff":
                    continue
                target.pause_state, target.pause_reason, target.paused_at = "system", incident.signal, now
                target.save(update_fields=["pause_state", "pause_reason", "paused_at", "updated_at"])
        if created or escalated or changed:
            _audit(incident, "detected", dry_run=dry_run, evidence_key=signal.evidence_key, scope=signal.scope, observed_at=now.isoformat())
        if not dry_run and signal.key == 'policy_block' and (created or changed):
            cache.delete('crm:deliverability:monitor:dns:' + str(stream.pk))
            cache.set('crm:deliverability:urgent-dns:' + str(stream.pk), True, timeout=policy.value('CRM_DNS_CHECK_SECONDS'))
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
        if getattr(incident, 'action', '') == 'alert_only':
            _audit(incident, 'acknowledged', actor=actor, resolution=resolution)
            return incident
        if resolution in ("resume", "resume_slow"):
            from crm.sending_domains import is_verified
            if not stream_configured(stream) or stream.transactional:
                if stream.transactional:
                    _audit(incident, "acknowledged", actor=actor, resolution=resolution)
                    return incident
                raise BlastError("stream_not_configured", 409)
            if not settings.CRM_SENDING_ENABLED or not stream.mailbox.enabled:
                raise BlastError("channel_disabled", 409)
            if not is_verified(stream.domain):
                raise BlastError("sending_system_unverified", 409)
            if stream.incidents.filter(state="open", severity="red").exclude(action="alert_only").exists():
                raise BlastError("other_incidents_open", 409)
            if (getattr(stream, 'state_detail', None) or {}).get('demoted_incident') != str(incident.pk):
                stream.ramp_stage = max(0, stream.ramp_stage - policy.value("CRM_GUARD_RESUME_DEMOTION"))
            stream.ramp_updated_at = now
            stream.state = "slowed" if resolution == "resume_slow" else "warming"
            stream.state_reason, stream.state_changed_at = "owner_resumed", now
            detail = dict(getattr(stream, 'state_detail', None) or {})
            detail.pop('incident_id', None)
            detail.pop('slow_until', None)
            if resolution == 'resume_slow':
                detail['slow_until'] = (now + timedelta(hours=policy.value('CRM_GUARD_RESUME_SLOW_HOURS'))).isoformat()
            stream.state_detail = detail
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
        from .guard_scopes import run_scopes, recover
        detected, firing = run_scopes(now=now)
        recover(firing, now=now)
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
