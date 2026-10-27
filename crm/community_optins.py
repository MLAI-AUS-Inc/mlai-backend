"""Import and continuously honour the community's explicit email choices."""

from collections import Counter

from django.db import transaction
from django.utils import timezone

from community_chat.onboarding_models import CommunityMemberConsent, CommunityMemberProfile
from core.models import AccountBan

from .emails import normalize_contact_email
from .models import Contact, ContactConsentEvent, CrmAuditEvent

SOURCE = "community_onboarding:"


def current_consent(profile):
    """Return the latest affirmative evidence only for a verified, active member."""
    user = profile.user
    if not (
        profile.status == CommunityMemberProfile.Status.APPROVED
        and profile.marketing_opt_in is True
        and user.is_active
        and user.email_verified_at
    ):
        return None
    evidence = CommunityMemberConsent.objects.filter(
        user=user, purpose="marketing_email"
    ).order_by("-created_at", "-pk").first()
    return evidence if evidence and evidence.granted else None


def contact_is_opted_in(contact):
    """Recheck the live source, address, CRM suppression and account ban before sending."""
    if (not contact.user_id or contact.email_status != Contact.EmailStatus.ACTIVE
            or contact.marketing_consent != Contact.MarketingConsent.GRANTED):
        return False
    profile = CommunityMemberProfile.objects.select_related("user").filter(user_id=contact.user_id).first()
    return bool(
        profile and current_consent(profile)
        and normalize_contact_email(profile.user.email) == contact.email
        and not AccountBan.objects.filter(email__iexact=contact.email, revoked_at__isnull=True).exists()
    )


def sync_community_optins(*, actor=None, dry_run=False):
    """Fill blanks and import affirmative evidence; never resurrect a suppressed contact."""
    counts = Counter()
    now = timezone.now()
    with transaction.atomic():
        profiles = CommunityMemberProfile.objects.select_related("user").filter(marketing_opt_in=True)
        for profile in profiles.iterator():
            evidence = current_consent(profile)
            email = normalize_contact_email(profile.user.email)
            if not evidence or not email:
                counts["ineligible"] += 1
                continue
            if AccountBan.objects.filter(email__iexact=email, revoked_at__isnull=True).exists():
                counts["suppressed"] += 1
                continue
            contact, created = Contact.objects.select_for_update().get_or_create(
                email=email, defaults={"source": Contact.Source.CHAT_ACCOUNT, "first_seen_at": now}
            )
            if (contact.email_status != Contact.EmailStatus.ACTIVE
                    or contact.marketing_consent == Contact.MarketingConsent.WITHDRAWN
                    or (contact.user_id and contact.user_id != profile.user_id)):
                counts["suppressed"] += 1
                continue
            fields = []
            for field, value in {
                "user_id": profile.user_id, "first_name": profile.first_name,
                "last_name": profile.last_name, "city": profile.city,
            }.items():
                if not getattr(contact, field) and value:
                    setattr(contact, field, value)
                    fields.append(field)
            if contact.marketing_consent != Contact.MarketingConsent.GRANTED:
                source = f"{SOURCE}{evidence.pk}"
                contact.marketing_consent = Contact.MarketingConsent.GRANTED
                contact.marketing_consent_source = source
                contact.marketing_consent_at = evidence.created_at
                fields += ["marketing_consent", "marketing_consent_source", "marketing_consent_at"]
                ContactConsentEvent.objects.create(
                    contact=contact, kind=ContactConsentEvent.Kind.GRANTED, source=source,
                    actor=actor, occurred_at=evidence.created_at,
                    detail={"profile_id": profile.pk, "consent_event_id": evidence.pk,
                            "policy_version": evidence.policy_version, "imported_at": now.isoformat()},
                )
                counts["consent_granted"] += 1
            if fields:
                contact.save(update_fields=fields + ["updated_at"])
            counts["created" if created else "updated" if fields else "unchanged"] += 1
        # Withdrawal of the original choice must also reach CRM/listmonk.
        from .listmonk_sync import record_email_opt_out
        for contact in Contact.objects.filter(
            marketing_consent_source__startswith=SOURCE, marketing_consent=Contact.MarketingConsent.GRANTED,
            email_status=Contact.EmailStatus.ACTIVE,
        ).select_related("user"):
            if not contact_is_opted_in(contact):
                record_email_opt_out(contact.email, "unsubscribed", source="community_onboarding:withdrawal", now=now)
                counts["withdrawn"] += 1
        if dry_run:
            transaction.set_rollback(True)
        elif any(counts[key] for key in ("created", "updated", "withdrawn")):
            CrmAuditEvent.objects.create(
                actor=actor, action=CrmAuditEvent.Action.IMPORT, target_type="community_optins",
                detail=dict(counts),
            )
    return dict(counts)
