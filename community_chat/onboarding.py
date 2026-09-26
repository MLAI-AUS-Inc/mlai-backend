"""Account-private onboarding, with server-authoritative community admission."""

import re
import secrets
import unicodedata

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, ValidationError

from .models import (
    CommunityChatAccountSession, CommunityChatDevice,
    CommunityMemberConsent, CommunityMemberProfile, CommunityMemberReviewRule,
)


INTERESTS = (
    ("llms_agents", "LLMs & agents"), ("building_ai", "Building with AI"),
    ("research", "Research"), ("ai_at_work", "AI in my work"),
    ("startups", "Startups"), ("exploring", "Just exploring"),
)
CITIES = (
    "Melbourne", "Sydney", "Brisbane", "Perth", "Adelaide", "Canberra",
    "Hobart", "Darwin", "Elsewhere in Australia", "Outside Australia", "Online only",
)
TERMS_URL = "https://mlai.au/terms"
PRIVACY_URL = "https://mlai.au/privacy"


def has_community_access(user):
    """An account's activation does not bypass a community admission decision."""
    if not user or not user.is_active:
        return False
    profile = CommunityMemberProfile.objects.filter(user=user).first()
    if profile is not None:
        return profile.status == CommunityMemberProfile.Status.APPROVED
    if not settings.COMMUNITY_CHAT_SIGNUP_ENABLED:
        return True
    # Preserve previously verified membership without inventing age or consent.
    return CommunityChatDevice.objects.filter(user=user, verified_at__isnull=False).exists()


def require_community_access(user):
    """Reject protected account and bootstrap operations before admission."""
    if not has_community_access(user):
        raise PermissionDenied({
            "error": "onboarding_required",
            "detail": "Complete your community application before joining MLAI Chat.",
        })


def onboarding_payload(user):
    """Project private answers only to their signed-in owner."""
    profile = CommunityMemberProfile.objects.filter(user=user).first()
    allowed = has_community_access(user)
    return {
        "available": settings.COMMUNITY_CHAT_SIGNUP_ENABLED or profile is not None,
        "required": not allowed,
        "status": profile.status if profile else ("approved" if allowed else "incomplete"),
        "basics_complete": bool(profile and profile.adult_confirmed_at
                                and "correction_requested" not in profile.review_reasons
                                and profile.policy_version == settings.COMMUNITY_CHAT_MEMBERSHIP_POLICY_VERSION),
        "policy_version": settings.COMMUNITY_CHAT_MEMBERSHIP_POLICY_VERSION,
        "terms_url": TERMS_URL,
        "code_of_conduct_url": TERMS_URL,
        "privacy_url": PRIVACY_URL,
        "first_name": profile.first_name if profile else user.first_name,
        "last_name": profile.last_name if profile else user.last_name,
        "email": user.email,
        "city": profile.city if profile else "",
        "interests": profile.interests if profile else [],
        "marketing_opt_in": profile.marketing_opt_in if profile else None,
        "cities": list(CITIES),
        "interest_options": [{"id": key, "label": label} for key, label in INTERESTS],
    }


def _normal_name(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def name_review_reasons(name):
    """Flag specific impersonation/spam signals, never unfamiliar name formats."""
    normalized = _normal_name(name)
    reasons = []
    if normalized in {"admin", "administrator", "mlai", "mlai admin", "mlai support", "roo"}:
        reasons.append("reserved_name")
    if re.search(r"https?://|www\.|(?:discord\.gg|t\.me)/", normalized):
        reasons.append("name_contains_link")
    for rule in CommunityMemberReviewRule.objects.filter(is_active=True).order_by("pk"):
        phrase = _normal_name(rule.phrase)
        if not phrase:
            continue
        matches = normalized == phrase if rule.match == "exact" else bool(
            re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", normalized)
        )
        if matches:
            reasons.append(f"rule:{rule.pk}")
    return reasons


def _locked_session_user(authenticated_session):
    user = get_user_model().objects.select_for_update().get(pk=authenticated_session.user_id)
    session = CommunityChatAccountSession.objects.select_for_update().filter(
        pk=authenticated_session.pk, user=user,
    ).first()
    now = timezone.now()
    if (session is None or not user.is_active or session.revoked_at is not None
            or session.auth_version != user.auth_version or session.access_expires_at <= now
            or session.expires_at <= now or not secrets.compare_digest(
                session.access_token_hash, authenticated_session.access_token_hash)):
        raise AuthenticationFailed("MLAI Chat session has expired.")
    return user, session


def _consent(user, purpose, granted, version, source):
    previous = CommunityMemberConsent.objects.filter(user=user, purpose=purpose).order_by("-pk").first()
    if previous and previous.granted == granted and previous.policy_version == version:
        return
    CommunityMemberConsent.objects.create(
        user=user, purpose=purpose, granted=granted, policy_version=version, source=source,
    )


def save_onboarding(*, authenticated_session, values):
    """Save one step idempotently while keeping account/session lock ordering."""
    with transaction.atomic():
        user, session = _locked_session_user(authenticated_session)
        if not settings.COMMUNITY_CHAT_SIGNUP_ENABLED:
            raise PermissionDenied("Community signup is temporarily unavailable.")
        existing_access = has_community_access(user)
        profile, _ = CommunityMemberProfile.objects.select_for_update().get_or_create(
            user=user, defaults={
                "status": CommunityMemberProfile.Status.APPROVED if existing_access else CommunityMemberProfile.Status.INCOMPLETE,
                "first_name": user.first_name, "last_name": user.last_name,
            },
        )
        if profile.status in {CommunityMemberProfile.Status.REJECTED, CommunityMemberProfile.Status.SUSPENDED}:
            raise PermissionDenied("Contact hi@mlai.au about your community application.")
        now = timezone.now()
        version = settings.COMMUNITY_CHAT_MEMBERSHIP_POLICY_VERSION
        step = values["step"]
        if step == "basics":
            if values.get("adult_confirmed") is not True or values.get("accept_rules") is not True:
                raise ValidationError({"detail": "Confirm that you are 18 or older and accept the community rules."})
            if values.get("policy_version") != version:
                raise ValidationError({"detail": "The community rules changed. Reload and review them again."})
            first_name = values.get("first_name", "").strip()
            last_name = values.get("last_name", "").strip()
            if not first_name:
                raise ValidationError({"first_name": "Enter the name we should use."})
            # Existing members manage their public name in Profile Settings.
            if existing_access and (first_name, last_name) != (user.first_name, user.last_name):
                raise ValidationError({"detail": "Change your public name in Profile Settings."})
            profile.first_name, profile.last_name = first_name, last_name
            profile.review_reasons = [reason for reason in profile.review_reasons if reason != "correction_requested"]
            profile.adult_confirmed_at = profile.adult_confirmed_at or now
            profile.policy_version = version
            for purpose in ("adult_eligibility", "terms", "code_of_conduct"):
                _consent(user, purpose, True, version, session.client_id)
        else:
            if not existing_access and (not profile.adult_confirmed_at or profile.policy_version != version
                    or "correction_requested" in profile.review_reasons):
                raise ValidationError({"detail": "Complete the required details first."})
            skipped = values.get("skip_personalisation", False)
            profile.personalisation_skipped = skipped
            if not skipped:
                profile.city = values.get("city", "")
                profile.interests = values.get("interests", [])
                if "marketing_opt_in" in values:
                    profile.marketing_opt_in = values["marketing_opt_in"]
                    _consent(user, "marketing_email", profile.marketing_opt_in, version, session.client_id)
            if not existing_access:
                profile.review_reasons = name_review_reasons(f"{profile.first_name} {profile.last_name}")
                profile.status = (CommunityMemberProfile.Status.PENDING if profile.review_reasons else CommunityMemberProfile.Status.APPROVED)
                profile.submitted_at = now
                if profile.status == CommunityMemberProfile.Status.APPROVED:
                    user.first_name, user.last_name = profile.first_name, profile.last_name
                    user.save(update_fields=("first_name", "last_name", "updated_at"))
        profile.save()
        return user
