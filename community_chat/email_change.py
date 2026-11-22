"""Two-inbox sign-in email changes for an installation-bound Chat account."""

import hmac
import json
import secrets
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import Throttled

from core.auth_throttles import RateLimit, client_ip, enforce_rate_limits
from core.email_aliases import lock_email_namespace
from core.email_change import commit_verified_email_locked, reserved_email
from .device_revocation import revoke_device_authority
from .email_codes import _hmac_hex, email_digest, invalidate_email_codes
from .email_delivery import decrypt_signup_email, encrypt_signup_email
from .models import (
    CommunityChatAccountSession, CommunityChatBootstrapToken, CommunityChatChallenge,
    CommunityChatDevice, CommunityChatDeviceAuthRequest, CommunityChatEmailChange,
    CommunityChatEmailChangeJob, DeviceBindingStatus, EmailChangeStatus,
)
from .onboarding_models import CommunityMemberProfile
from .permissions import chat_role

OPEN = (EmailChangeStatus.CURRENT, EmailChangeStatus.NEW)
RETIRED_REASON = "sign_in_email_changed"
CHANGE_TTL = timedelta(minutes=20)
RECEIPT_TTL = timedelta(hours=24)


def available_for(user):
    """Return server-side eligibility for self-service."""
    return (
        settings.COMMUNITY_CHAT_EMAIL_CHANGE_ENABLED
        and not settings.MLAI_LEGACY_EMAIL_MAGIC_LINKS_ENABLED
        and bool(settings.SYSTEM_EMAIL_RESEND_API_KEY)
        and user.is_active
        and not user.is_staff
        and not user.is_superuser
        and not reserved_email(user.email)
        and chat_role(user) == "member"
    )


def retired_signer(public_key):
    """Prevent re-enrollment of a key pending or already remotely revoked."""
    return CommunityChatEmailChangeJob.objects.filter(kind="relay", public_key=public_key).exists()


def lock_signer(public_key):
    """Serialize pending retirement with new signer's enrollment."""
    lock_email_namespace(f"chat-signer:{public_key}")


def _mask(email):
    local, _, domain = email.rpartition("@")
    return f"{local[:1]}***@{domain}"


def _code_digest(change, code):
    return _hmac_hex("account-email-change-code", f"{change.pk}:{change.status}:{change.generation}:{code}")


def _limits(request, user, session, action, recipient_digests=()):
    window = 3600 if action == "send" else 600
    per_account, per_ip = (8, 100) if action == "send" else (20, 150)
    enforce_rate_limits([
        RateLimit(action, "account", user.pk, per_account, window),
        RateLimit(action, "installation", session.installation_id, per_account, window),
        RateLimit(action, "ip", client_ip(request), per_ip, window),
        *(RateLimit(action, "recipient", digest, 8, window) for digest in set(recipient_digests)),
    ], namespace="chat-email-change")


def _mail(change, key, kind, recipient, now, *, code=None, expires_at=None, target=None):
    CommunityChatEmailChangeJob.objects.create(
        change=change, key=key, kind=kind, generation=change.generation,
        ciphertext=encrypt_signup_email(json.dumps({"recipient": recipient, "code": code, "target": target})),
        expires_at=expires_at or now + RECEIPT_TTL, available_at=now,
    )


def _discard_code_jobs(change, now):
    CommunityChatEmailChangeJob.objects.filter(
        change=change, kind__in=("current_code", "new_code"), finished_at__isnull=True,
    ).update(ciphertext="", finished_at=now, claimed_at=None)


def finish(change, status, now):
    """End a pending change and erase its verification material."""
    change.status = status
    change.finished_at = now
    change.new_email_ciphertext = ""
    change.code_digest = ""
    change.old_digest = ""
    change.replay = {}
    change.save()
    _discard_code_jobs(change, now)


def _issue_code(change, recipient, target, now):
    _discard_code_jobs(change, now)
    old_proof = change.replay.get("verified_current_proof")
    while True:
        code = f"{secrets.randbelow(1_000_000):06d}"
        if (not old_proof or _hmac_hex("email-change-current-proof", f"{change.pk}:{code}") != old_proof
        ) and not hmac.compare_digest(change.code_digest, _code_digest(change, code)):
            break
    change.generation += 1
    change.code_digest = _code_digest(change, code)
    change.code_expires_at = min(
        change.expires_at, now + timedelta(seconds=settings.COMMUNITY_CHAT_EMAIL_CODE_TTL_SECONDS),
    )
    change.resend_at = min(
        change.expires_at, now + timedelta(seconds=settings.COMMUNITY_CHAT_EMAIL_CODE_RESEND_SECONDS),
    )
    change.save()
    kind = "current_code" if change.status == EmailChangeStatus.CURRENT else "new_code"
    _mail(change, f"code-{change.generation}", kind, recipient, now, code=code,
          expires_at=change.code_expires_at, target=_mask(target))


def _payload(change, user):
    if change is None:
        return {"status": "idle", "current_email": user.email}
    return {
        "status": change.status,
        "request_id": str(change.pk),
        "current_email": user.email,
        "new_email_masked": _mask(decrypt_signup_email(change.new_email_ciphertext)),
        "expires_at": change.expires_at,
        "code_expires_at": change.code_expires_at,
        "resend_at": change.resend_at,
    }


def _revoke_previous_credentials(change, user, new_email, now):
    signing_keys = set()
    for model in (CommunityChatAccountSession, CommunityChatBootstrapToken):
        active = model.objects.filter(user=user, revoked_at__isnull=True)
        signing_keys.update(active.values_list("public_key", flat=True))
        active.update(revoked_at=now)
    CommunityChatDeviceAuthRequest.objects.filter(
        user=user, consumed_at__isnull=True, expires_at__gt=now,
    ).update(expires_at=now)
    CommunityChatChallenge.objects.filter(user=user, used_at__isnull=True).update(used_at=now)
    invalidate_email_codes(
        Q(user=user) | Q(user__isnull=True, email_digest__in=(email_digest(user.email), email_digest(new_email))),
        now=now,
    )

    devices = CommunityChatDevice.objects.filter(
        user=user, status__in=(DeviceBindingStatus.PENDING, DeviceBindingStatus.VERIFIED),
    ).order_by("pk")
    devices = list(devices)
    signing_keys.update(device.public_key for device in devices)
    for public_key in sorted(signing_keys - {""}):
        lock_signer(public_key)
    for device in devices:
        invite_ids = []
        revoked = revoke_device_authority(
            user, device_id=device.pk, public_key=device.public_key, reason=RETIRED_REASON,
            revoke_member_invite_callback=invite_ids.append,
        )
        if revoked is None:
            raise RuntimeError("Device changed while holding the account lock")
        CommunityChatEmailChangeJob.objects.create(
            change=change, key=f"relay-{device.public_key}", kind="relay",
            public_key=device.public_key, invite_ids=invite_ids, available_at=now,
        )
        signing_keys.discard(device.public_key)

    # Enrollment may have minted an upstream invite before its local write
    # rolled back. Fence these scoped keys even if there was no device record.
    other_keys = CommunityChatDevice.objects.filter(
        public_key__in=signing_keys, status__in=(DeviceBindingStatus.PENDING, DeviceBindingStatus.VERIFIED),
    ).exclude(user=user).values_list("public_key", flat=True)
    for public_key in sorted(signing_keys.difference(other_keys, {""})):
        CommunityChatEmailChangeJob.objects.create(
            change=change, key=f"relay-{public_key}", kind="relay",
            public_key=public_key, available_at=now,
        )

    # A fresh marketing choice will create new consent evidence. Preserve
    # address-level suppressions and the historical consent and contact rows.
    CommunityMemberProfile.objects.filter(user=user, marketing_opt_in=True).update(marketing_opt_in=False)


def _retire_old_address_invites(email, now):
    # Previously accepted memberships are ID-bound. Open address-only links
    # must not be usable by the next account to claim the retired address.
    from founder_tools.models import StartupTeamInvitation, StartupTeamInviteDelivery
    from mentoring.models import MentorInvite

    ids = list(StartupTeamInvitation.objects.filter(
        email__iexact=email, accepted_at__isnull=True, revoked_at__isnull=True,
    ).values_list("pk", flat=True))
    StartupTeamInvitation.objects.filter(pk__in=ids, accepted_at__isnull=True).update(revoked_at=now)
    StartupTeamInviteDelivery.objects.filter(
        invitation_id__in=ids,
        state__in=(StartupTeamInviteDelivery.State.QUEUED, StartupTeamInviteDelivery.State.SENDING),
    ).update(
        state=StartupTeamInviteDelivery.State.CANCELLED, leased_at=None, context={},
    )
    MentorInvite.objects.filter(
        email__iexact=email, user__isnull=True, used_at__isnull=True, revoked_at__isnull=True,
    ).update(revoked_at=now)



def apply_action(request, user, session, action, values, idempotency_key):
    """Run one step under the user and live-session locks held by the view."""
    now = timezone.now()
    change = CommunityChatEmailChange.objects.select_for_update().filter(user=user, status__in=OPEN).first()
    if change and (
        change.expires_at <= now or change.auth_version != user.auth_version
        or change.session_id is None or change.session.revoked_at is not None
        or change.session.expires_at <= now
        or not hmac.compare_digest(change.old_digest, email_digest(user.email))
    ):
        finish(change, EmailChangeStatus.EXPIRED, now)
        change = None
    if action == "status":
        return 200, _payload(change if change and change.session_id == session.pk else None, user)

    fingerprint = _hmac_hex("account-email-change-replay", json.dumps(values, sort_keys=True, default=str))
    replay_key = f"{action}:{idempotency_key}"
    if action == "start":
        if change and change.session_id == session.pk and replay_key in change.replay:
            if change.replay[replay_key][0] != fingerprint:
                return 409, {"error": "request_changed"}
            return 202, _payload(change, user)
        if change:
            return 409, {"error": "change_already_pending"}
        new_email = values["new_email"]
        if new_email == user.email or reserved_email(new_email):
            return 400, {"error": "invalid_new_email"}
        _limits(request, user, session, "send", (email_digest(user.email),))
        change = CommunityChatEmailChange.objects.create(
            user=user, session=session, auth_version=user.auth_version, old_digest=email_digest(user.email),
            new_email_ciphertext=encrypt_signup_email(new_email), status=EmailChangeStatus.CURRENT,
            expires_at=now + CHANGE_TTL, code_expires_at=now, resend_at=now,
            replay={replay_key: [fingerprint, None]},
        )
        _issue_code(change, user.email, new_email, now)
        return 202, _payload(change, user)

    if not change or change.session_id != session.pk or change.pk != values["request_id"]:
        return 400, {"error": "invalid_or_expired_request"}
    if action == "cancel":
        finish(change, EmailChangeStatus.CANCELLED, now)
        return 200, _payload(None, user)
    if replay_key in change.replay:
        previous, error = change.replay[replay_key]
        if previous != fingerprint:
            return 409, {"error": "request_changed"}
        return (400, {"error": error}) if error else (200, _payload(change, user))

    new_email = decrypt_signup_email(change.new_email_ciphertext)
    if action == "resend":
        if now < change.resend_at:
            raise Throttled(wait=(change.resend_at - now).total_seconds())
        recipient = user.email if change.status == EmailChangeStatus.CURRENT else new_email
        _limits(request, user, session, "send", (email_digest(recipient),))
        change.replay[replay_key] = [fingerprint, None]
        _issue_code(change, recipient, new_email, now)
        return 200, _payload(change, user)

    step = EmailChangeStatus.CURRENT if action == "verify-current" else EmailChangeStatus.NEW
    if change.status != step:
        return 409, {"error": "request_changed"}
    if now >= change.code_expires_at:
        return 400, {"error": "code_expired"}
    _limits(request, user, session, "verify")
    if not hmac.compare_digest(change.code_digest, _code_digest(change, values["code"])):
        change.attempts += 1
        if change.attempts >= settings.COMMUNITY_CHAT_EMAIL_CODE_MAX_ATTEMPTS:
            finish(change, EmailChangeStatus.EXPIRED, now)
            return 400, {"error": "invalid_or_expired_request"}
        change.replay[replay_key] = [fingerprint, "invalid_code"]
        change.save(update_fields=("attempts", "replay"))
        return 400, {"error": "invalid_code"}

    if step == EmailChangeStatus.CURRENT:
        _limits(request, user, session, "send", (email_digest(new_email),))
        change.replay[replay_key] = [fingerprint, None]
        change.replay["verified_current_proof"] = _hmac_hex(
            "email-change-current-proof", f"{change.pk}:{values['code']}",
        )
        change.status = EmailChangeStatus.NEW
        change.attempts = 0
        _issue_code(change, new_email, new_email, now)
        return 200, _payload(change, user)

    if not commit_verified_email_locked(user, new_email, now=now):
        finish(change, EmailChangeStatus.CONFLICT, now)
        return 409, {"error": "address_unavailable"}
    _revoke_previous_credentials(change, user, new_email, now)
    _retire_old_address_invites(user.email, now)
    finish(change, EmailChangeStatus.COMPLETED, now)
    _mail(change, "receipt-old", "receipt", user.email, now, target=_mask(new_email))
    _mail(change, "receipt-new", "receipt", new_email, now, target=_mask(new_email))
    return 200, {"status": "reauthentication_required"}
