"""Company-owned source defaults without changing or deleting account credentials."""
from django.db import transaction
from rest_framework.exceptions import ValidationError

from organizations.models import Organization
from startup_updates.models import StartupProfile
from .lifecycle import UPDATE_PROVIDERS

ACTIVITY_WINDOW_DAYS = 30
PREFERENCE_KEY = "chat_source_preferences"


def source_preferences(company):
    """Read only this company's source choices from its existing configuration."""
    if not company.organization_id:
        return {}
    profile = StartupProfile.objects.filter(organization_id=company.organization_id).first()
    configuration = profile.progress_configuration if profile else {}
    choices = (configuration or {}).get(PREFERENCE_KEY, {}).get(str(company.pk), {})
    return {key: value for key, value in choices.items() if key in UPDATE_PROVIDERS and isinstance(value, bool)}


@transaction.atomic
def set_source_preference(company, provider, value):
    """Persist a toggle independently of connection state and other companies."""
    if provider not in UPDATE_PROVIDERS:
        raise ValidationError({"provider": "Choose a supported connection."})
    if not isinstance(value, bool):
        raise ValidationError({"enabled": "Choose on or off."})
    if not company.organization_id:
        raise ValidationError({"companyId": "Finish setting up this startup first."})
    Organization.objects.select_for_update().get(pk=company.organization_id)
    profile, _ = StartupProfile.objects.select_for_update().get_or_create(organization_id=company.organization_id)
    configuration = dict(profile.progress_configuration or {})
    companies = dict(configuration.get(PREFERENCE_KEY) or {})
    choices = dict(companies.get(str(company.pk)) or {})
    choices[provider] = value
    companies[str(company.pk)] = choices
    configuration[PREFERENCE_KEY] = companies
    profile.progress_configuration = configuration
    profile.save(update_fields=["progress_configuration", "updated_at"])
    return {"provider": provider, "enabled": value, "activityWindowDays": ACTIVITY_WINDOW_DAYS}
