"""Current optional review preferences, without progress-cache or settings writes."""
from django.conf import settings


def review_preferences(*, organization, user):
    """Read voice independently of article editing rights and never change a draft.

    Only the current active settings owner can edit future voice preferences,
    after first-draft eligibility was actually earned. Saving that preference
    and explicitly regenerating a draft remain separate commands.
    """
    from django.contrib.auth import get_user_model
    from founder_tools import startup_team
    from founder_tools.services import user_may_use_organization
    from .article_voice import article_voice_payload
    from .models import OrganizationContentConfig
    from .p2_foundation import p2_applies
    from .founder_improvements import KEY, has_first_draft
    config = OrganizationContentConfig.objects.filter(organization_id=getattr(organization, 'pk', None)).first()
    strategy = getattr(config, 'pillar_strategy', None)
    strategy = strategy if isinstance(strategy, dict) else {}
    value = article_voice_payload(strategy)
    can_edit = False
    fresh_user = get_user_model().objects.filter(pk=getattr(user, 'pk', None), is_active=True).first()
    if (config is not None and fresh_user is not None
            and getattr(settings, 'MY_STARTUP_ARTICLE_IMPROVEMENTS_ENABLED', False)
            and p2_applies(organization) and user_may_use_organization(fresh_user, organization)):
        roster = startup_team._roster(config)
        valid_owner = not (startup_team.KEY in strategy and roster is None or roster and roster.get('frozen'))
        heal = strategy.get('self_heal')
        prior = heal.get(KEY) if isinstance(heal, dict) else None
        earned = (isinstance(prior, dict) and type(prior.get('version')) is int
            and prior['version'] == 1 and prior.get('available') is True)
        can_edit = bool(valid_owner and (earned or has_first_draft(organization)))
    return {'voice': {'canEdit': can_edit, 'value': value}}
