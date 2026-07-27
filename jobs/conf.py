from __future__ import annotations

from django.conf import settings as django_settings


class JobsSettings:
    @property
    def public_base_url(self) -> str:
        return str(getattr(django_settings, "JOBS_PUBLIC_BASE_URL", getattr(django_settings, "DEFAULT_BACKEND_URL", "http://localhost:8000")) or "http://localhost:8000").rstrip("/")

    @property
    def jobs_trigger_token(self) -> str | None:
        value = getattr(django_settings, "JOBS_TRIGGER_TOKEN", None)
        return str(value).strip() if value else None

    @property
    def jobs_scheduler_enabled(self) -> bool:
        return bool(getattr(django_settings, "JOBS_SCHEDULER_ENABLED", False))

    @property
    def jobs_schedule_timezone(self) -> str:
        return str(getattr(django_settings, "JOBS_SCHEDULE_TIMEZONE", "Australia/Melbourne") or "Australia/Melbourne")

    @property
    def jobs_schedule_hour(self) -> int:
        return int(getattr(django_settings, "JOBS_SCHEDULE_HOUR", 7))

    @property
    def jobs_schedule_minute(self) -> int:
        return int(getattr(django_settings, "JOBS_SCHEDULE_MINUTE", 0))

    @property
    def jobs_retry_attempts(self) -> int:
        return int(getattr(django_settings, "JOBS_RETRY_ATTEMPTS", 3))

    @property
    def jobs_retry_delay_seconds(self) -> int:
        return int(getattr(django_settings, "JOBS_RETRY_DELAY_SECONDS", 300))

    @property
    def jobs_failure_stop_after_days(self) -> int:
        return int(getattr(django_settings, "JOBS_FAILURE_STOP_AFTER_DAYS", 3))

    @property
    def jobs_scheduler_post_to_slack(self) -> bool:
        return bool(getattr(django_settings, "JOBS_SCHEDULER_POST_TO_SLACK", True))

    @property
    def jobs_scheduler_post_to_notion(self) -> bool:
        return bool(getattr(django_settings, "JOBS_SCHEDULER_POST_TO_NOTION", True))

    @property
    def jobs_scheduler_max_pages(self) -> int:
        return int(getattr(django_settings, "JOBS_SCHEDULER_MAX_PAGES", 1))

    @property
    def jobs_scheduler_per_keyword_limit(self) -> int:
        return int(getattr(django_settings, "JOBS_SCHEDULER_PER_KEYWORD_LIMIT", 5))

    @property
    def jobs_scrape_headless(self) -> bool:
        return bool(getattr(django_settings, "JOBS_SCRAPE_HEADLESS", True))

    @property
    def jobs_seek_max_pages(self) -> int:
        return int(getattr(django_settings, "JOBS_SEEK_MAX_PAGES", 3))

    @property
    def jobs_seek_per_keyword_limit(self) -> int:
        return int(getattr(django_settings, "JOBS_SEEK_PER_KEYWORD_LIMIT", 12))

    @property
    def jobs_freshness_hours(self) -> int:
        return int(getattr(django_settings, "JOBS_FRESHNESS_HOURS", 72))

    @property
    def jobs_top_pick_limit(self) -> int:
        return int(getattr(django_settings, "JOBS_TOP_PICK_LIMIT", 3))

    @property
    def notion_top_pick_limit(self) -> int:
        return int(getattr(django_settings, "JOBS_NOTION_TOP_PICK_LIMIT", 7))

    @property
    def notion_api_token(self) -> str | None:
        value = getattr(django_settings, "JOBS_NOTION_API_TOKEN", None)
        return str(value).strip() if value else None

    @property
    def notion_parent_page_id(self) -> str | None:
        value = getattr(django_settings, "JOBS_NOTION_PARENT_PAGE_ID", None)
        return str(value).strip() if value else None

    @property
    def notion_api_version(self) -> str:
        return str(getattr(django_settings, "JOBS_NOTION_API_VERSION", "2022-06-28"))

    @property
    def slack_jobs_channel(self) -> str:
        return str(getattr(django_settings, "JOBS_SLACK_CHANNEL", "#jobs") or "#jobs")

    @property
    def slack_webhook_url(self) -> str | None:
        value = getattr(django_settings, "JOBS_SLACK_WEBHOOK_URL", None)
        return str(value).strip() if value else None

    @property
    def slack_bot_token(self) -> str | None:
        value = getattr(django_settings, "SLACK_BOT_TOKEN", None) or getattr(django_settings, "JOBS_SLACK_BOT_TOKEN", None)
        return str(value).strip() if value else None

    @property
    def llm_judge_enabled(self) -> bool:
        return bool(getattr(django_settings, "JOBS_LLM_JUDGE_ENABLED", False))

    @property
    def llm_judge_api_key(self) -> str | None:
        value = getattr(django_settings, "JOBS_LLM_JUDGE_API_KEY", None) or getattr(django_settings, "OPENAI_API_KEY", None)
        return str(value).strip() if value else None

    @property
    def llm_judge_base_url(self) -> str:
        return str(getattr(django_settings, "JOBS_LLM_JUDGE_BASE_URL", "https://api.openai.com/v1"))

    @property
    def llm_judge_model(self) -> str:
        return str(getattr(django_settings, "JOBS_LLM_JUDGE_MODEL", "gpt-4o-mini"))

    @property
    def llm_location_check_enabled(self) -> bool:
        return bool(getattr(django_settings, "JOBS_LLM_LOCATION_CHECK_ENABLED", True))


settings = JobsSettings()
