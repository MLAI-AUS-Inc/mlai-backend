"""Company-owned source defaults without changing or deleting account credentials."""
from django.db import transaction
from rest_framework.exceptions import ValidationError

from founder_tools.models import VibeRaisingCompany
from organizations.models import Organization
from startup_updates.models import StartupProfile
from .lifecycle import UPDATE_PROVIDERS, OAUTH_PROVIDERS, API_KEY_PROVIDERS, source_capabilities

PREFERENCE_KEY = "chat_source_preferences"


def _choices(company, companies):
    creator_key = f"{company.pk}:{company.profile.user_id}" if company.profile_id else None
    if company.membership_policy == VibeRaisingCompany.SHARED_ACCESS:
        return companies.get(str(company.pk), companies.get(creator_key, {}))
    return companies.get(creator_key, {}) if creator_key else {}



def source_preferences(company):
    """Read only this company's source choices from its existing configuration."""
    if not company.organization_id:
        return {}
    profile = StartupProfile.objects.filter(organization_id=company.organization_id).first()
    configuration = profile.progress_configuration if profile else {}
    choices = _choices(company, (configuration or {}).get(PREFERENCE_KEY, {}))
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
    choices = dict(_choices(company, companies))
    scope_key = (str(company.pk) if company.membership_policy == VibeRaisingCompany.SHARED_ACCESS
                 else f"{company.pk}:{company.profile.user_id}")
    choices[provider] = value
    companies[scope_key] = choices
    configuration[PREFERENCE_KEY] = companies
    profile.progress_configuration = configuration
    profile.save(update_fields=["progress_configuration", "updated_at"])
    return {"provider": provider, "enabled": value, "activityWindowDays": None, "activityPeriod": "reporting_month"}
