"""Website authorization status, separate from sources included in Pulse."""
from django.conf import settings

from content_factory.activation import github_account_state
from content_factory.google_baseline import google_connection_has_baseline_scope
from content_factory.models import OrganizationContentConfig
from founder_tools.services import actor_ids_for_user
from integrations.services.external_connectors import google_connection_for_org, is_provider_configured
from integrations.services.github_installations import user_github_installations


def website_connection_sources(user, company):
    """Project the selected startup's saved account state without claiming access."""
    organization = company.organization
    google = google_connection_for_org(user, organization) if organization else None
    google_ready = google_connection_has_baseline_scope(google)
    google_configured = is_provider_configured("gmail")
    github_configured = bool(getattr(settings, "GITHUB_OAUTH_CLIENT_ID", "")
        and getattr(settings, "GITHUB_OAUTH_CLIENT_SECRET", ""))
    config = OrganizationContentConfig.objects.filter(organization=organization).first() if organization else None
    account = github_account_state(config, actor_ids=actor_ids_for_user(user), installations=user_github_installations(user))
    github_status = account["status"]
    # Saved access remains visible even if the server's OAuth initiation is disabled.
    if not account["saved"] and (not github_configured or not organization):
        github_status = "unavailable"
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
            "status": github_status, "accountLabel": account["accountLabel"],
            "repositorySelected": bool(config and config.github_repo), "repositoryAccessVerified": False,
            "warning": ("Checking your saved GitHub access. Open website setup to verify it."
                if github_status == "checking" else "Review your GitHub access."
                if github_status == "needs_action" else "GitHub is not available yet."
                if github_status == "unavailable" and organization else
                "Add your startup website in Startup details first." if not organization else
                "Choose a repository in website setup." if account["saved"] and not (config and config.github_repo) else None)},
    ]
