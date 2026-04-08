from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from django.db import migrations, models


DEFAULT_SCHEDULE_TIMEZONE = "Australia/Melbourne"
DEFAULT_SCHEDULE_START_HOUR = 8
DEFAULT_SCHEDULE_START_MINUTE = 0
DEFAULT_SCHEDULE_SLOT_MINUTES = 15


def _normalize_domain(value):
    domain = str(value or "").strip().lower()
    if domain.startswith("https://"):
        domain = domain[8:]
    elif domain.startswith("http://"):
        domain = domain[7:]
    if domain.startswith("www."):
        domain = domain[4:]
    if "/" in domain:
        domain = domain.split("/", 1)[0]
    return domain


def _record_unique_mapping(mapping, key, value):
    normalized_key = str(key or "").strip()
    normalized_value = str(value or "").strip()
    if not normalized_key or not normalized_value:
        return
    if normalized_key in mapping and mapping[normalized_key] != normalized_value:
        mapping[normalized_key] = None
    elif normalized_key not in mapping:
        mapping[normalized_key] = normalized_value


def _dispatch_scheduled_for(local_date, slot_index):
    local_dt = datetime.combine(
        local_date,
        time(hour=DEFAULT_SCHEDULE_START_HOUR, minute=DEFAULT_SCHEDULE_START_MINUTE),
        tzinfo=ZoneInfo(DEFAULT_SCHEDULE_TIMEZONE),
    )
    local_dt += timedelta(minutes=max(0, int(slot_index or 0)) * DEFAULT_SCHEDULE_SLOT_MINUTES)
    return local_dt.astimezone(timezone.utc)


def backfill_daily_discovery_fields(apps, schema_editor):
    OrganizationContentConfig = apps.get_model("core", "OrganizationContentConfig")
    ScheduledDiscoveryDispatch = apps.get_model("core", "ScheduledDiscoveryDispatch")
    ContentFactoryJob = apps.get_model("core", "ContentFactoryJob")
    UserIntegration = apps.get_model("integrations", "UserIntegration")

    repo_to_user = {}
    github_user_to_slack = {}
    domain_to_recent_owner = {}
    historical_domains = set()

    for integration in UserIntegration.objects.all():
        _record_unique_mapping(
            repo_to_user,
            getattr(integration, "github_repo", ""),
            getattr(integration, "slack_user_id", ""),
        )
        _record_unique_mapping(
            github_user_to_slack,
            getattr(integration, "github_user_name", ""),
            getattr(integration, "slack_user_id", ""),
        )

    for dispatch in ScheduledDiscoveryDispatch.objects.exclude(domain="").exclude(slack_user_id="").order_by("-updated_at"):
        normalized_domain = _normalize_domain(getattr(dispatch, "domain", ""))
        slack_user_id = str(getattr(dispatch, "slack_user_id", "") or "").strip()
        if normalized_domain:
            historical_domains.add(normalized_domain)
        if normalized_domain and slack_user_id and normalized_domain not in domain_to_recent_owner:
            domain_to_recent_owner[normalized_domain] = slack_user_id
        if getattr(dispatch, "scheduled_for_at", None) is None:
            dispatch.scheduled_for_at = _dispatch_scheduled_for(
                getattr(dispatch, "local_date", None),
                getattr(dispatch, "slot_index", 0),
            )
            dispatch.save(update_fields=["scheduled_for_at"])

    for job in ContentFactoryJob.objects.exclude(domain="").exclude(slack_user_id="").order_by("-updated_at"):
        request_meta = getattr(job, "request_meta", {}) or {}
        if str(request_meta.get("trigger_source") or "").strip() != "scheduled_daily":
            continue
        if str(request_meta.get("source_run_id") or "").strip():
            continue
        normalized_domain = _normalize_domain(getattr(job, "domain", ""))
        slack_user_id = str(getattr(job, "slack_user_id", "") or "").strip()
        if normalized_domain:
            historical_domains.add(normalized_domain)
        if normalized_domain and slack_user_id and normalized_domain not in domain_to_recent_owner:
            domain_to_recent_owner[normalized_domain] = slack_user_id

    for config in OrganizationContentConfig.objects.select_related("organization").all():
        domain = _normalize_domain(getattr(getattr(config, "organization", None), "domain", ""))
        owner = str(getattr(config, "connected_slack_user_id", "") or "").strip()
        if not owner:
            github_repo = str(getattr(config, "github_repo", "") or "").strip()
            github_user_name = str(getattr(config, "github_user_name", "") or "").strip()
            owner = (
                repo_to_user.get(github_repo)
                or github_user_to_slack.get(github_user_name)
                or domain_to_recent_owner.get(domain)
                or ""
            )

        update_fields = []
        if owner and owner != str(getattr(config, "connected_slack_user_id", "") or "").strip():
            config.connected_slack_user_id = owner
            update_fields.append("connected_slack_user_id")
        if domain and domain in historical_domains and not getattr(config, "daily_discovery_enabled", False):
            config.daily_discovery_enabled = True
            update_fields.append("daily_discovery_enabled")
        if update_fields:
            config.save(update_fields=update_fields)


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0008_alter_gmailattachmentartifact_content_disposition_and_more"),
        ("core", "0046_alter_contentfactoryrun_status_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="organizationcontentconfig",
            name="daily_discovery_enabled",
            field=models.BooleanField(
                default=False,
                help_text="Whether this organization participates in the shared daily discovery queue.",
            ),
        ),
        migrations.AddField(
            model_name="organizationcontentconfig",
            name="daily_discovery_priority",
            field=models.PositiveSmallIntegerField(
                default=0,
                help_text="Lower numbers run earlier in the shared daily discovery queue.",
            ),
        ),
        migrations.AddField(
            model_name="scheduleddiscoverydispatch",
            name="scheduled_for_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="scheduleddiscoverydispatch",
            name="slot_index",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AlterField(
            model_name="scheduleddiscoverydispatch",
            name="state",
            field=models.CharField(
                choices=[
                    ("scheduled", "Scheduled"),
                    ("queued", "Queued"),
                    ("topic_selection_sent", "Topic Selection Sent"),
                    ("confirmed", "Confirmed"),
                    ("cancelled", "Cancelled"),
                    ("failed", "Failed"),
                    ("failed_timeout", "Failed Timeout"),
                    ("expired", "Expired"),
                ],
                db_index=True,
                default="scheduled",
                max_length=30,
            ),
        ),
        migrations.RunPython(
            backfill_daily_discovery_fields,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
