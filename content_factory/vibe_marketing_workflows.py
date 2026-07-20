"""Canonical workflow names that belong to the Vibe Marketing product."""

VIBE_MARKETING_WORKFLOWS = frozenset(
    {
        "repo_scan",
        "content_factory_scan",
        "article_system_setup",
        "auto_discovery",
        "content_factory_discovery",
        "article_generation",
        "content_factory_article",
        "direct_generate",
        "confirmed_topic",
        "article_revision",
        "daily_discovery",
        "startup_autofill",
        "website_baseline",
        "vibe_marketing_daily_replay",
    }
)

DISCOVERY_WORKFLOWS = frozenset(
    {"auto_discovery", "content_factory_discovery", "daily_discovery"}
)
