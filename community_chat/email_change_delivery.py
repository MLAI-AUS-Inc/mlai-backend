"""Drain expired proofs, security mail and durable relay revocations."""

import json
import logging
from datetime import timedelta

from cryptography.fernet import InvalidToken
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from system_email import api as system_email
from system_email.transport_resend import SystemEmailRejected
from .adapter import revoke_member_invite, revoke_relay_membership
from .email_change import OPEN, finish
from .email_delivery import DELIVERY_LEASE_SECONDS, MAX_DELIVERY_ATTEMPTS, _lock, decrypt_signup_email
from .models import CommunityChatEmailChange, CommunityChatEmailChangeJob, EmailChangeStatus

logger = logging.getLogger(__name__)
CODE_KINDS = {"current_code", "new_code"}


def _expire(limit, now):
    stale = CommunityChatEmailChange.objects.filter(status__in=OPEN)
    if settings.COMMUNITY_CHAT_EMAIL_CHANGE_ENABLED and not settings.MLAI_LEGACY_EMAIL_MAGIC_LINKS_ENABLED:
        stale = stale.filter(expires_at__lte=now)
    with transaction.atomic():
        for change in _lock(stale).order_by("expires_at")[:limit]:
            finish(change, EmailChangeStatus.EXPIRED, now)
    CommunityChatEmailChangeJob.objects.filter(
        expires_at__lte=now, finished_at__isnull=True,
    ).update(ciphertext="", claimed_at=None, finished_at=now)
    CommunityChatEmailChange.objects.filter(
        finished_at__lt=now - timedelta(days=90),
    ).exclude(status=EmailChangeStatus.COMPLETED).exclude(communitychatemailchangejob__finished_at__isnull=True).delete()


def _claim(now):
    stale = now - timedelta(seconds=DELIVERY_LEASE_SECONDS)
    with transaction.atomic():
        job = _lock(CommunityChatEmailChangeJob.objects.filter(
            finished_at__isnull=True, available_at__lte=now,
        ).filter(Q(claimed_at__isnull=True) | Q(claimed_at__lt=stale))).order_by("available_at", "pk").first()
        if job:
            job.claimed_at = now
            job.attempts += 1
            job.save(update_fields=("claimed_at", "attempts"))
        return job


def _send(job):
    if job.kind == "relay":
        for invite_id in job.invite_ids:
            revoke_member_invite(invite_id)
        revoke_relay_membership(job.public_key)
        return
    if job.expires_at <= timezone.now():
        return
    change = job.change
    if job.kind in CODE_KINDS and (
        not settings.COMMUNITY_CHAT_EMAIL_CHANGE_ENABLED or settings.MLAI_LEGACY_EMAIL_MAGIC_LINKS_ENABLED
        or change.status not in OPEN or change.generation != job.generation
        or not change.session_id or change.session.revoked_at is not None
        or change.session.expires_at <= timezone.now()
        or change.user.auth_version != change.auth_version
    ):
        return
    data = json.loads(decrypt_signup_email(job.ciphertext))
    system_email.send(
        f"chat_email_change_{job.kind}", to=data["recipient"],
        idempotency_key=f"chat-email-change/{job.change_id}/{job.key}",
        user_id=change.user_id,
        context={"verification_code": data["code"], "new_email_masked": data["target"]},
    )


def process_email_change_jobs(*, limit):
    """Process one bounded batch without taking a database lock during I/O."""
    now = timezone.now()
    _expire(limit, now)
    processed = 0
    for _ in range(limit):
        job = _claim(timezone.now())
        if job is None:
            break
        processed += 1
        claimed = CommunityChatEmailChangeJob.objects.filter(
            pk=job.pk, claimed_at=job.claimed_at, finished_at__isnull=True,
        )
        if not claimed.exists():
            continue
        try:
            _send(job)
        except Exception as exc:
            logger.warning("Email change job failed job_id=%s kind=%s error=%s", job.pk, job.kind, type(exc).__name__)
            if job.kind == "relay" or (
                job.attempts < MAX_DELIVERY_ATTEMPTS and not isinstance(exc, (InvalidToken, SystemEmailRejected))
            ):
                delay = min(300, 2 ** min(job.attempts, 9))
                claimed.update(claimed_at=None, available_at=timezone.now() + timedelta(seconds=delay))
                continue
        claimed.update(claimed_at=None, finished_at=timezone.now(), ciphertext="")
    return processed
