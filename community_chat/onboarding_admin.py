"""Committee review through Django's permissioned, audited administration UI."""

import logging
from functools import partial

from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from integrations import http_client

from .models import CommunityMemberConsent, CommunityMemberProfile, CommunityMemberReviewRule
from .onboarding import name_is_complete

logger = logging.getLogger(__name__)

REVIEW_CLEARED_SUBJECT = "You're in"
REVIEW_CLEARED_BODY = "Your MLAI membership is approved. You can now post in the community."
REVIEW_CLEARED_FROM = "MLAI <notifications@mlai.au>"
# Remember a sent "You're in" for a year; the provider key dedupes retries too.
REVIEW_CLEARED_GUARD_SECONDS = 365 * 24 * 3600


def review_cleared_email_enabled():
    """Whether approving a pending member emails them. Code default off."""
    return bool(getattr(settings, "COMMUNITY_CHAT_REVIEW_CLEARED_EMAIL_ENABLED", False))


def review_cleared_idempotency_key(member_id):
    """One "You're in" per member, whatever retries or re-reviews happen."""
    return f"chat-review-cleared/{member_id}"


def send_review_cleared_email(member_id, user_id):
    """Send one "You're in" email through Resend, at most once per member.

    Runs after the approval commits. Never raises: a failed send is logged with
    ids only and the guard is released, so the approval itself always stands.
    """
    key = review_cleared_idempotency_key(member_id)
    guard = f"community-chat:{key}"
    if not cache.add(guard, "sent", REVIEW_CLEARED_GUARD_SECONDS):
        return False
    user = get_user_model().objects.filter(pk=user_id, is_active=True).only("email").first()
    api_key = str(getattr(settings, "RESEND_API_KEY", "") or "").strip()
    if user is None or not user.email or not api_key:
        cache.delete(guard)
        logger.warning("community_chat_review_cleared_email_skipped member=%s configured=%s",
                       member_id, bool(api_key))
        return False
    try:
        response = http_client.post(
            "https://api.resend.com/emails",
            json={"from": REVIEW_CLEARED_FROM, "to": [user.email],
                  "subject": REVIEW_CLEARED_SUBJECT, "text": REVIEW_CLEARED_BODY},
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                     "Idempotency-Key": key},
            timeout=(3, 8),
        )
        sent = response.status_code < 300
    except Exception as exc:  # The approval stands; log the failure, ids only.
        sent = False
        logger.warning("community_chat_review_cleared_email_failed member=%s error_type=%s",
                       member_id, exc.__class__.__name__)
    if not sent:
        cache.delete(guard)
        return False
    logger.info("community_chat_review_cleared_email_sent member=%s", member_id)
    return True


@admin.register(CommunityMemberProfile)
class CommunityMemberProfileAdmin(admin.ModelAdmin):
    """Review pending applications without editing or fabricating consent."""

    list_display = ("user_id", "first_name", "last_name", "status", "submitted_at", "reviewed_at")
    list_filter = ("status", "city")
    search_fields = ("user__email", "first_name", "last_name")
    readonly_fields = tuple(field.name for field in CommunityMemberProfile._meta.fields)
    actions = ("approve_applications", "request_correction", "reject_applications")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def _review(self, request, queryset, status):
        reviewed = 0
        for candidate in queryset.order_by("user_id"):
            with transaction.atomic():
                user = get_user_model().objects.select_for_update().get(pk=candidate.user_id)
                profile = CommunityMemberProfile.objects.select_for_update().get(pk=candidate.pk)
                if profile.status != CommunityMemberProfile.Status.PENDING:
                    continue
                if status == CommunityMemberProfile.Status.APPROVED and (
                    not user.is_active or not user.email_verified_at or not profile.adult_confirmed_at
                    or not name_is_complete(profile.first_name, profile.last_name)
                    or profile.policy_version != settings.COMMUNITY_CHAT_MEMBERSHIP_POLICY_VERSION
                ):
                    continue
                profile.status = status
                if status == CommunityMemberProfile.Status.INCOMPLETE:
                    profile.review_reasons = [*profile.review_reasons, "correction_requested"]
                profile.reviewed_at = timezone.now()
                profile.reviewed_by = request.user
                profile.save(update_fields=("status", "review_reasons", "reviewed_at", "reviewed_by", "updated_at"))
                if status == CommunityMemberProfile.Status.APPROVED:
                    user.first_name, user.last_name = profile.first_name, profile.last_name
                    user.save(update_fields=("first_name", "last_name", "updated_at"))
                    if review_cleared_email_enabled():
                        # Only a real pending_review -> approved transition gets here.
                        transaction.on_commit(partial(send_review_cleared_email, profile.pk, user.pk))
                LogEntry.objects.log_action(
                    user_id=request.user.pk,
                    content_type_id=ContentType.objects.get_for_model(CommunityMemberProfile).pk,
                    object_id=profile.pk, object_repr=f"Community application {profile.pk}",
                    action_flag=CHANGE, change_message=f"Membership review: {status}",
                )
                reviewed += 1
        self.message_user(request, f"Updated {reviewed} pending application(s).", messages.SUCCESS)

    @admin.action(description="Approve eligible pending applications")
    def approve_applications(self, request, queryset):
        self._review(request, queryset, CommunityMemberProfile.Status.APPROVED)

    @admin.action(description="Ask pending applicants to correct their details")
    def request_correction(self, request, queryset):
        self._review(request, queryset, CommunityMemberProfile.Status.INCOMPLETE)

    @admin.action(description="Reject pending applications (help/appeal remains available)")
    def reject_applications(self, request, queryset):
        self._review(request, queryset, CommunityMemberProfile.Status.REJECTED)


@admin.register(CommunityMemberConsent)
class CommunityMemberConsentAdmin(admin.ModelAdmin):
    """Consent evidence is visible to authorized staff but not editable."""

    list_display = ("user_id", "purpose", "granted", "policy_version", "created_at")
    list_filter = ("purpose", "granted")
    readonly_fields = tuple(field.name for field in CommunityMemberConsent._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CommunityMemberReviewRule)
class CommunityMemberReviewRuleAdmin(admin.ModelAdmin):
    list_display = ("phrase", "match", "reason", "is_active")
    list_filter = ("is_active", "match")
    readonly_fields = ("created_at",)
