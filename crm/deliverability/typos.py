"""Manual known-domain corrections, retaining every consent and delivery hold."""

import copy

from django.db import IntegrityError, connection, transaction

from community_chat.admin_access import require_mlai_admin
from core.models import AccountBan
from crm.blasts import BlastError
from crm.email_aliases import canonical_emails, invalidate
from crm.emails import normalize_contact_email
from crm.luma_sync import users_by_email
from crm.models import Contact, CrmAuditEvent
from crm.queries import banned

from . import hygiene, policy


def suggestion(address):
    local, separator, domain = address.rpartition('@')
    corrected = hygiene.TYPO.get(domain.lower())
    return local + separator + corrected if corrected else None


def correct(contact_id, *, expected, replacement, actor, now):
    """Apply only the displayed known typo after an explicit human action."""
    require_mlai_admin(actor)
    if not policy.value('CRM_LIFECYCLE_ENABLED'):
        raise BlastError('lifecycle_disabled', 404)
    if connection.vendor != 'postgresql':
        raise BlastError('contact_review_required', 409)
    try:
        with transaction.atomic():
            person = Contact.objects.select_for_update().annotate(is_banned=banned()).get(pk=contact_id)
            address = normalize_contact_email(replacement)
            if expected != person.email:
                raise BlastError('contact_changed', 409)
            if not address or address != suggestion(person.email):
                raise BlastError('invalid_email_correction')
            if person.is_banned or AccountBan.objects.filter(email__iexact=address, revoked_at__isnull=True).exists():
                raise BlastError('email_banned', 409)
            if Contact.objects.filter(email=address).exists() or canonical_emails([address]):
                raise BlastError('contact_exists', 409)
            if not isinstance(person.attributes, dict):
                raise BlastError('contact_review_required', 409)
            attrs = copy.deepcopy(person.attributes)
            aliases = attrs.get('email_aliases', [])
            if not isinstance(aliases, list) or len(aliases) >= 5 or any(not isinstance(value, str) or not normalize_contact_email(value) for value in aliases):
                raise BlastError('contact_review_required', 409)
            attrs['email_aliases'] = list(dict.fromkeys([*aliases, person.email]))
            person.email, person.attributes = address, attrs
            person.listmonk_subscriber_id = None
            target_user = users_by_email([address]).get(address)
            if person.user_id and target_user and person.user_id != target_user.pk:
                raise BlastError('contact_review_required', 409)
            person.user = person.user if person.user_id else target_user
            person.save(update_fields=['email', 'attributes', 'listmonk_subscriber_id', 'user', 'updated_at'])
            invalidate()
            transaction.on_commit(invalidate)
            CrmAuditEvent.objects.create(actor=actor, action='edit', target_type='contact', target_id=str(person.pk),
                detail={'operation': 'email_typo_corrected', 'fields': ['email']})
            CrmAuditEvent.objects.create(actor=actor, action='edit', target_type='crm_email_aliases', target_id=str(person.pk),
                detail={'operation': 'email_correction_committed'})
            return person
    except IntegrityError:
        raise BlastError('contact_exists', 409) from None
