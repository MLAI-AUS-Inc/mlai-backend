"""Hand off one startup that still points to a departing account."""

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from founder_tools.membership import active_memberships, record_event, remove
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization

from .deletion_tasks import deletion_pending
from .models import AccountDeletionRequest


@transaction.atomic
def handoff_startup(*, request_id, company_id, successor_id, apply):
    """Transfer ownership to an explicitly approved existing active founder."""
    case = AccountDeletionRequest.objects.filter(pk=request_id).first()
    if (
        case is None
        or case.scope != AccountDeletionRequest.Scope.ACCOUNT
        or case.status == AccountDeletionRequest.Status.COMPLETED
        or case.user_id is None
        or case.user_id == successor_id
    ):
        raise ValueError("An open whole-account request and a different successor are required.")
    users = get_user_model().objects.select_for_update().filter(
        pk__in=(case.user_id, successor_id),
    ).order_by("pk")
    users = {user.pk: user for user in users}
    if len(users) != 2 or not users[successor_id].is_active or deletion_pending(successor_id):
        raise ValueError("The reviewed successor is unavailable.")
    current_case = AccountDeletionRequest.objects.select_for_update().get(pk=request_id)
    if current_case.status == AccountDeletionRequest.Status.COMPLETED or current_case.user_id != case.user_id:
        raise ValueError("The deletion case changed.")

    company = VibeRaisingCompany.objects.filter(pk=company_id).first()
    if company is None or company.organization_id is None:
        raise ValueError("The startup has no matching organisation.")
    Organization.objects.select_for_update().get(pk=company.organization_id)
    company = VibeRaisingCompany.objects.select_for_update().get(pk=company_id)
    if company.membership_policy != VibeRaisingCompany.SHARED_ACCESS or company.closed_at is not None:
        raise ValueError("Legacy or closed startups need an individual handoff.")
    successor = VibeRaisingProfile.objects.filter(
        user_id=successor_id, role=VibeRaisingProfile.ROLE_FOUNDER,
    ).first()
    if successor is None or not active_memberships(company).filter(user_id=successor_id).exists():
        raise ValueError("Choose an active founder who already belongs to this startup.")
    membership = company.memberships.filter(user_id=case.user_id, removed_at__isnull=True).first()
    if company.profile_id == successor.pk and membership is None:
        return "already_transferred"
    if not VibeRaisingProfile.objects.filter(pk=company.profile_id, user_id=case.user_id).exists():
        raise ValueError("The departing account is not the recorded startup owner.")
    if not apply:
        return "ready"
    company.profile = successor
    company.save(update_fields=("profile", "updated_at"))
    if membership is not None:
        remove(company, membership, actor=None, now=timezone.now())
    record_event(company, "deletion_handoff", subject=users[successor_id])
    return "transferred"
