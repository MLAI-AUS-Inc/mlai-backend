"""Committee review through Django's permissioned, audited administration UI."""

from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

from .models import CommunityMemberConsent, CommunityMemberProfile, CommunityMemberReviewRule


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
                    or not profile.first_name or profile.policy_version != settings.COMMUNITY_CHAT_MEMBERSHIP_POLICY_VERSION
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
