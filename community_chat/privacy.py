"""Account privacy controls with explicit disclosure and authority boundaries."""

import hashlib
import json
import secrets
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, ValidationError

from .models import AccountDeletionRequest, AiConsentRecord, CommunityChatAccountSession


def ai_disclosure():
    """Return public operator-confirmed recipients; fail closed if unconfigured."""
    providers = getattr(settings, "COMMUNITY_CHAT_AI_PROVIDERS", [])
    version = str(getattr(settings, "COMMUNITY_CHAT_AI_DISCLOSURE_VERSION", "")).strip()
    valid = isinstance(providers, list) and bool(providers) and bool(version)
    valid = valid and all(
        isinstance(provider, dict)
        and set(provider) == {"name", "purpose", "data", "privacy_url"}
        and all(isinstance(value, str) and value.strip() for value in provider.values())
        and provider["privacy_url"].startswith("https://")
        for provider in providers
    )
    if not valid:
        return {"available": False, "version": "", "providers": [], "provider_digest": ""}
    digest = hashlib.sha256(json.dumps(providers, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"available": True, "version": version, "providers": providers, "provider_digest": digest}


def has_ai_consent(user_id):
    """Check current consent at dispatch time, including disclosure changes."""
    disclosure = ai_disclosure()
    return disclosure["available"] and AiConsentRecord.objects.filter(
        user_id=user_id, purpose="roo_chat", user__is_active=True,
        disclosure_version=disclosure["version"],
        provider_digest=disclosure["provider_digest"],
        granted_at__isnull=False, withdrawn_at__isnull=True,
    ).exists()


@contextmanager
def locked_privacy_session(authenticated_session):
    """Serialize privacy writes with credential rotation and device revocation."""
    with transaction.atomic():
        user = get_user_model().objects.select_for_update().get(pk=authenticated_session.user_id)
        session = CommunityChatAccountSession.objects.select_for_update().filter(
            pk=authenticated_session.pk, user=user,
        ).first()
        now = timezone.now()
        if (session is None or not user.is_active or session.revoked_at is not None
                or session.auth_version != user.auth_version or session.expires_at <= now
                or session.access_expires_at <= now
                or not secrets.compare_digest(session.access_token_hash, authenticated_session.access_token_hash)):
            raise AuthenticationFailed("MLAI Chat session has expired.")
        yield user, session


def set_ai_consent(*, authenticated_session, granted, version, provider_digest):
    """Withdraw at any time; require the exact visible disclosure when granting."""
    with locked_privacy_session(authenticated_session) as (user, _):
        disclosure = ai_disclosure()
        if granted and (not disclosure["available"] or version != disclosure["version"]
                        or provider_digest != disclosure["provider_digest"]):
            raise ValidationError({"code": "ai_disclosure_changed", "detail": "Reload the AI disclosure before agreeing."})
        now = timezone.now()
        record, _ = AiConsentRecord.objects.get_or_create(user=user, purpose="roo_chat")
        if granted:
            record.disclosure_version = version
            record.provider_digest = provider_digest
            record.granted_at = now
            record.withdrawn_at = None
        else:
            record.withdrawn_at = now
        record.save()
        return record


def deletion_policy():
    """Expose deletion only after an operator has committed to handling requests."""
    timeframe = str(getattr(settings, "COMMUNITY_CHAT_DELETION_TIMEFRAME", "")).strip()
    owner = str(getattr(settings, "COMMUNITY_CHAT_DELETION_CONTACT", "")).strip()
    return {"available": bool(timeframe and owner), "version": "2026-09-20",
            "timeframe": timeframe, "contact": owner}


def request_account_deletion(*, authenticated_session, scope, policy_version):
    """Record a confirmed request once; reauthentication protects new requests."""
    policy = deletion_policy()
    if not policy["available"]:
        raise ValidationError({"code": "deletion_unavailable", "detail": "Account deletion is not configured."})
    if scope not in AccountDeletionRequest.Scope.values or policy_version != policy["version"]:
        raise ValidationError({"code": "deletion_policy_changed", "detail": "Reload the deletion information."})
    with locked_privacy_session(authenticated_session) as (user, session):
        existing = AccountDeletionRequest.objects.filter(user=user, scope=scope).exclude(
            status=AccountDeletionRequest.Status.COMPLETED,
        ).first()
        if existing:
            from .deletion_tasks import schedule_deletion
            schedule_deletion(existing)
            return existing, False
        if session.created_at < timezone.now() - timedelta(minutes=10):
            raise PermissionDenied({"code": "reauthentication_required", "detail": "Sign in again before requesting deletion."})
        record = AccountDeletionRequest.objects.create(user=user, scope=scope, policy_version=policy_version)
        from .deletion_tasks import schedule_deletion
        schedule_deletion(record)
        return record, True
