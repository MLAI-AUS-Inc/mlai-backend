"""Staff explanations for automatic campaign actions."""

REASONS = {
    "hard_bounce_rate": "a lot of the addresses didn't exist",
    "complaint_rate": "more people than usual marked it as spam",
    "policy_block": "an email provider started rejecting our messages",
    'unsubscribe_rate': 'unusually many people unsubscribed',
    'postmaster_spam_rate': 'more people than usual marked it as spam',
    'blocklist': 'an email provider started rejecting our messages',
    'seed_spam': 'our inbox checks found a problem',
}


def status_message(blast, *, now=None):
    """Keep internal diagnostics and provider configuration out of staff copy."""
    from django.utils import timezone
    from crm.blast_email import parse_iso
    from . import volume
    now = now or timezone.now()
    status = getattr(blast, 'status', '')
    stats = getattr(blast, 'stats', None) or {}
    email = stats.get('email') or {}
    if status in ('sent', 'partially_sent'):
        return {'tone': 'ok', 'text': f"Finished. Delivered to {email.get('delivered', 0):,} people."}
    if status == 'cancelled':
        sent = email.get('sent', 0)
        remaining = max(0, (getattr(blast, 'recipient_count', 0) or 0) - sent)
        return {'tone': 'stop', 'text': f"Stopped by {stats.get('stopped_by') or 'staff'}. {sent:,} emails were sent; {remaining:,} won't be sent."}
    if getattr(blast, "pause_state", "") == "system":
        reason = REASONS.get(blast.pause_reason, "we need to check how these emails are being received")
        return {"tone": "stop", "text": f"Paused to protect MLAI's email reputation: {reason}. The email team will decide what happens next."}
    if getattr(blast, "pause_state", "") == "staff":
        actor = getattr(blast, 'paused_by', None)
        name = (getattr(actor, 'full_name', '') or '').strip() or 'staff'
        return {"tone": "warn", "text": f"Paused by {name}. Nothing more goes out until someone resumes it."}
    preflight = getattr(blast, 'preflight', None) or {}
    if getattr(blast, 'status', '') in ('queued', 'sending') and preflight:
        if (preflight.get('seed_test') or {}).get('status') in ('sending', 'waiting'):
            return {'tone': 'info', 'text': "Starting soon. We're running a quick inbox test first, usually within 10 minutes."}
        if 'seed_test' in (preflight.get('blocking') or []):
            return {'tone': 'stop', 'text': 'The inbox check found a problem. The email team needs to review it before sending.'}
    if getattr(blast, "stream_id", None) and blast.stream.state == "stopped":
        return {"tone": "stop", "text": "Sending is paused while the email team checks a problem."}
    if getattr(blast, "stream_id", None) and blast.stream.state == "held":
        return {"tone": "warn", "text": "Paused for a moment while we reconnect to our email service. It will restart by itself."}
    if (getattr(blast, "status", "") in ("queued", "sending")
            and ((getattr(blast, "stats", None) or {}).get("email") or {}).get("resume_pending")):
        return {"tone": "info", "text": "Ready to continue. Sending will restart after the next email system check."}
    if status not in ('queued', 'sending') or not getattr(blast, 'stream_id', None):
        return None
    if blast.stream.state == 'slowed':
        return {'tone': 'info', 'text': "Sending a little more slowly today to keep MLAI's emails out of spam folders. It will continue during planned sending times."}
    providers = (blast.stream.state_detail or {}).get('providers') or {}
    for provider, state in sorted(providers.items()):
        if state.get('state') in ('held', 'throttled'):
            name = {'google': 'Gmail', 'microsoft': 'Outlook', 'yahoo': 'Yahoo', 'apple': 'Apple Mail'}.get(provider, 'An email provider')
            return {'tone': 'info', 'text': f'{name} asked us to slow down, so people with those addresses will get it a bit later.'}
    deferred = email.get('frequency_deferred') or 0
    if deferred:
        return {'tone': 'info', 'text': f'{deferred:,} people were emailed recently, so their copies will go once enough time has passed.'}
    if not volume.window(blast.stream, now, critical=volume.time_critical(blast, now)):
        return {'tone': 'info', 'text': 'Waiting for the next planned sending time.'}
    slots = ((getattr(blast, 'plan', None) or {}).get('estimate') or {}).get('slots') or []
    upcoming = [(parse_iso(row.get('at')), row.get('count')) for row in slots]
    for stamp, count in upcoming:
        if stamp and stamp >= now and type(count) is int and count > 0:
            when = stamp.astimezone(volume.MELBOURNE).strftime('%A at %-I:%M%p').replace('AM', 'am').replace('PM', 'pm')
            return {'tone': 'info', 'text': f'Next batch: about {count:,} people, {when}.'}
    return {'tone': 'info', 'text': 'Sending gradually, with the most active members first.'}
    return None
