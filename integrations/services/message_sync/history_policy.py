"""Provider request size follows the verified Slack distribution budget."""
from django.conf import settings


def history_page_limit():
    """Use Slack's recommended page size only for verified Tier 3 apps."""
    distribution = getattr(settings, "MESSAGE_SYNC_SLACK_DISTRIBUTION", "restricted")
    return 200 if distribution in {"internal", "marketplace"} else 15
