"""Staff explanations for automatic campaign actions."""

REASONS = {
    "hard_bounce_rate": "a lot of the addresses didn't exist",
    "complaint_rate": "more people than usual marked it as spam",
    "policy_block": "an email provider started rejecting our messages",
}


def status_message(blast):
    """Keep internal diagnostics and provider configuration out of staff copy."""
    if getattr(blast, "pause_state", "") == "system":
        reason = REASONS.get(blast.pause_reason, "we need to check how these emails are being received")
        return {"tone": "stop", "text": f"Paused to protect MLAI's email reputation: {reason}. The email team will decide what happens next."}
    if getattr(blast, "pause_state", "") == "staff":
        return {"tone": "warn", "text": "Paused by staff. Nothing more goes out until someone resumes it."}
    preflight = getattr(blast, 'preflight', None) or {}
    if getattr(blast, 'status', '') in ('queued', 'sending') and preflight:
        if (preflight.get('seed_test') or {}).get('status') in ('sending', 'waiting'):
            return {'tone': 'info', 'text': "Starting soon. We're running a quick inbox test first, usually within 10 minutes."}
        if 'seed_test' in (preflight.get('blocking') or []):
            return {'tone': 'stop', 'text': 'The inbox check found a problem. The email team needs to review it before sending.'}
    if getattr(blast, "stream_id", None) and blast.stream.state == "stopped":
        return {"tone": "stop", "text": "Sending is paused while the email team checks a problem."}
    if getattr(blast, "stream_id", None) and blast.stream.state == "held":
        return {"tone": "warn", "text": "Paused while we check our email service. Your email will wait."}
    if (getattr(blast, "status", "") in ("queued", "sending")
            and ((getattr(blast, "stats", None) or {}).get("email") or {}).get("resume_pending")):
        return {"tone": "info", "text": "Ready to continue. Sending will restart after the next email system check."}
    return None
