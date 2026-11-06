"""Preserve a manually corrected contact's identity on later imports and opt-outs."""

from django.core.cache import cache
from django.db.models import Max

from .emails import normalize_contact_email
from .models import Contact, CrmAuditEvent

CACHE_KEY = 'crm:corrected-email-aliases:v1'


def invalidate():
    """Called after a manual correction commits; failure cannot undo a saved correction."""
    try:
        cache.delete(CACHE_KEY)
    except Exception:
        pass


def canonical_emails(emails):
    """Return only requested, validated aliases; an exact current address takes precedence."""
    requested = set(emails)
    if not requested:
        return {}
    version = CrmAuditEvent.objects.filter(target_type='crm_email_aliases').aggregate(n=Max('pk'))['n'] or 0
    try:
        saved = cache.get(CACHE_KEY)
    except Exception:
        saved = None
    if not isinstance(saved, dict) or saved.get('version') != version or not isinstance(saved.get('aliases'), dict):
        aliases_by_email = {}
        for current, attrs in Contact.objects.filter(attributes__email_aliases__isnull=False).values_list('email', 'attributes'):
            aliases = attrs.get('email_aliases') if isinstance(attrs, dict) else None
            if not isinstance(aliases, list):
                continue
            for value in aliases[:5]:
                alias = normalize_contact_email(value) if isinstance(value, str) else None
                if alias and alias != current:
                    if alias in aliases_by_email and aliases_by_email[alias] != current:
                        raise ValueError('Conflicting contact email corrections require review')
                    aliases_by_email[alias] = current
        saved = {'version': version, 'aliases': aliases_by_email}
        try:
            cache.set(CACHE_KEY, saved, timeout=3600)
        except Exception:
            pass
    found = {alias: current for alias, current in saved['aliases'].items() if alias in requested}
    if found:
        for direct in Contact.objects.filter(email__in=found).values_list('email', flat=True):
            found.pop(direct, None)
    return found
