"""Configuration checks for Content Factory billing flags."""
from django.conf import settings
from django.core.checks import Tags, Warning, register


@register(Tags.compatibility)
def free_first_article_goal_check(app_configs, **kwargs):
    """Warn when the free first article and the first-article goal are both on.

    The goal-scoped path charges its own spend, so P0 refuses to combine them:
    the free first article stays off while the goal flag is on.
    """
    if getattr(settings, "ARTICLES_FREE_FIRST_ARTICLE_ENABLED", False) and getattr(
            settings, "ARTICLES_FIRST_ARTICLE_GOAL_ENABLED", False):
        return [Warning(
            "ARTICLES_FREE_FIRST_ARTICLE_ENABLED has no effect while ARTICLES_FIRST_ARTICLE_GOAL_ENABLED is on.",
            hint="Turn ARTICLES_FIRST_ARTICLE_GOAL_ENABLED off to offer the free first article.",
            id="content_factory.W001",
        )]
    return []
