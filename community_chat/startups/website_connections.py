"""Website authorizations displayed beside update sources, without draft opt-in."""
from django.conf import settings

from content_factory.google_baseline import google_connection_has_baseline_scope
from content_factory.models import OrganizationContentConfig
from founder_tools.services import actor_ids_for_user
from integrations.services.external_connectors import google_connection_for_org, is_provider_configured


def website_connection_sources(user, company):
    """Read only the selected startup's saved Google/GitHub authorization."""
    organization = company.organization
    google = google_connection_for_org(user, organization) if organization else None
    google_ready = google_connection_has_baseline_scope(google)
    google_configured = is_provider_configured("gmail")
    github_configured = bool(getattr(settings, "GITHUB_OAUTH_CLIENT_ID", "")
        and getattr(settings, "GITHUB_OAUTH_CLIENT_SECRET", ""))
    config = OrganizationContentConfig.objects.filter(
        organization=organization, connected_slack_user_id__in=actor_ids_for_user(user),
    ).first() if organization else None
    github_ready = bool(config and config.github_installation_id
        and config.github_connection_state in {"connected", "repo_selection_required"})
    common = {"connectMode": "oauth", "canDisconnect": False, "usableForUpdates": False,
        "enabled": False, "selected": False, "capabilities": ["website"],
        "activityWindowDays": 30, "selectionMode": "recent_activity"}
    return [
        {**common, "key": "google_search_console", "provider": "google_search_console",
            "label": "Google Search Console", "configured": google_configured,
            "canConnect": google_configured,
            "status": "connected" if google_ready else "not_connected" if google_configured else "unavailable",
            "accountLabel": google.google_email if google else None,
            "warning": None if google_configured else "Google Search Console is not available yet."},
        {**common, "key": "github", "provider": "github", "label": "GitHub",
            "configured": github_configured, "canConnect": bool(github_configured and organization),
            "status": "connected" if github_ready else "not_connected" if github_configured and organization else "unavailable",
            "accountLabel": config.github_user_name if github_ready else None,
            "warning": ("GitHub is not available yet." if not github_configured else
                "Choose a repository in website setup to publish articles."
                if github_ready and not config.github_repo else None)
                if organization else "Add your startup website in Startup details first."},
    ]
