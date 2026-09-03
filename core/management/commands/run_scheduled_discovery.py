import json
import logging
from datetime import date as calendar_date

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from content_analytics.services.report_scheduler import run_daily_article_report_scheduler
from content_factory.reconciliation import run_content_factory_reconciliation_sweep
from content_factory.services.island_refresh_scheduler import run_island_refresh_scheduler
from integrations.services.daily_discovery import (
    enqueue_scheduled_discovery,
    run_daily_discovery_scheduler,
)
from integrations.services.github_installations import (
    run_github_installation_reconciliation_sweep,
)
from integrations.services.research_automations import run_research_automation_scheduler
from integrations.services.xero_reconciliation import run_daily_payout_reconciliation
from jobs.services.job_pipeline import run_daily_jobs_scheduler
from hospital.sim_retention import run_scheduled_sim_conversation_cleanup
from roo.coding import reconcile_coding_reservations
from roo.office_manager import run_office_manager_scheduler
from roo.models import ScheduledDiscoveryHeartbeat
from startup_updates.monthly_update_reminders import run_monthly_update_reminder_scheduler

logger = logging.getLogger(__name__)


_OFFICE_MANAGER_DELIVERY_BOOLEAN_KEYS = frozenset(
    {
        "announcement_sent",
        "message_updated",
        "winner_channel_announcement_sent",
        "winner_dm_sent",
        "end_of_day_reminder_sent",
    }
)
_OFFICE_MANAGER_DELIVERY_CONTAINER_KEYS = frozenset(
    {
        "delivery_results",
        "delivery_statuses",
        "recovered_deliveries",
        "winner_channel_retractions",
    }
)
_OFFICE_MANAGER_FAILURE_CONTAINER_KEYS = frozenset(
    {
        "delivery_failures",
        "failed_deliveries",
        "exhausted_deliveries",
    }
)
_OFFICE_MANAGER_TERMINAL_DELIVERY_STATES = frozenset(
    {
        "dead_letter",
        "dead-letter",
        "exhausted",
        "failed",
        "failure",
        "permanent_failure",
        "permanent-failure",
        "terminal_failure",
        "terminal-failure",
    }
)
_OFFICE_MANAGER_DELIVERY_KEY_PARTS = (
    "announcement",
    "delivery",
    "message",
    "reminder",
    "retraction",
    "winner_dm",
)


def _office_manager_delivery_value_failed(value, *, delivery_context=False) -> bool:
    """Return whether an explicit delivery result reports a required failure."""
    if value is False and delivery_context:
        return True
    if isinstance(value, str) and delivery_context:
        return value.strip().lower() in _OFFICE_MANAGER_TERMINAL_DELIVERY_STATES
    if isinstance(value, dict):
        for raw_key, nested_value in value.items():
            key = str(raw_key).strip().lower()
            if key in _OFFICE_MANAGER_FAILURE_CONTAINER_KEYS and nested_value:
                return True
            nested_delivery_context = delivery_context or (
                key in _OFFICE_MANAGER_DELIVERY_CONTAINER_KEYS
                or key in _OFFICE_MANAGER_DELIVERY_BOOLEAN_KEYS
                or any(part in key for part in _OFFICE_MANAGER_DELIVERY_KEY_PARTS)
            )
            if _office_manager_delivery_value_failed(
                nested_value,
                delivery_context=nested_delivery_context,
            ):
                return True
        return False
    if isinstance(value, (list, tuple, set)):
        return any(
            _office_manager_delivery_value_failed(
                item,
                delivery_context=delivery_context,
            )
            for item in value
        )
    return False


def _office_manager_scheduler_failed(result) -> bool:
    """Keep business states non-fatal while propagating required I/O failure."""
    if not isinstance(result, dict):
        return True
    if str(result.get("status") or "").strip().lower() in (
        _OFFICE_MANAGER_TERMINAL_DELIVERY_STATES
    ):
        return True
    return _office_manager_delivery_value_failed(result)


class Command(BaseCommand):
    help = "Run the scheduled daily discovery selector or enqueue one specific replay target."

    def add_arguments(self, parser):
        parser.add_argument("--domain", help="Specific domain to enqueue.")
        parser.add_argument("--slack-user-id", help="Specific Slack user ID to enqueue.")
        parser.add_argument(
            "--local-date",
            help="Override target local date using YYYY-MM-DD.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Replay an existing terminal dispatch for the same user/domain/local date.",
        )

    def handle(self, *args, **options):
        domain = options.get("domain")
        slack_user_id = options.get("slack_user_id")
        local_date_raw = options.get("local_date")
        force = bool(options.get("force"))

        if bool(domain) != bool(slack_user_id):
            raise CommandError("--domain and --slack-user-id must be provided together.")

        local_date = None
        if local_date_raw:
            try:
                local_date = calendar_date.fromisoformat(str(local_date_raw))
            except ValueError as exc:
                raise CommandError("--local-date must use YYYY-MM-DD format.") from exc

        if domain and slack_user_id:
            result = enqueue_scheduled_discovery(
                domain=domain,
                slack_user_id=slack_user_id,
                local_date=local_date,
                force=force,
            )
            self.stdout.write(json.dumps(result, sort_keys=True))
            if result.get("status") == "failed":
                raise CommandError(result.get("error") or "Scheduled discovery enqueue failed.")
            return

        heartbeat, _ = ScheduledDiscoveryHeartbeat.objects.get_or_create(
            name="scheduled_discovery",
        )
        heartbeat.last_started_at = timezone.now()
        heartbeat.last_error = ""
        heartbeat.save(update_fields=["last_started_at", "last_error", "updated_at"])

        results = {}
        failures = []

        for name, runner in (
            ("daily_discovery", run_daily_discovery_scheduler),
            ("jobs", run_daily_jobs_scheduler),
            # Drives the daily research-topic email/Slack/WhatsApp send (8am slot).
            # Idempotent per run, so it is safe to tick every scheduler loop.
            ("research_automations", run_research_automation_scheduler),
            # Closes runs whose content-factory callbacks were permanently
            # lost (outbox retries exhaust after ~2h) and dispatch ghosts.
            # Self-throttling per run, so it is safe to tick every loop.
            ("content_factory_reconciliation", run_content_factory_reconciliation_sweep),
            # Prunes stale GitHub App installations (founder uninstalled the
            # App) from the founder registry so dead rows stop poisoning the
            # "registry exists" guards. Self-throttling (min-age + probe
            # interval + batch cap), so it is safe to tick every loop.
            ("github_installation_reconciliation", run_github_installation_reconciliation_sweep),
            # Refreshes the durable Stripe payout ledger once per local day.
            # This never posts to Xero; posting always requires admin approval.
            ("stripe_payout_reconciliation", run_daily_payout_reconciliation),
            # Enforces the configured raw-dialogue retention window once per
            # local day. Its cache marker makes the minute scheduler tick cheap.
            ("health_hack_conversation_retention", run_scheduled_sim_conversation_cleanup),
            # Generates each analytics-enabled org's immutable daily article
            # performance brief once per org-local date after the configured
            # local hour. Gated by CONTENT_ANALYTICS_REPORTS_ENABLED; the
            # report's unique constraint makes every later tick a cheap
            # existence check.
            ("article_performance_reports", run_daily_article_report_scheduler),
            # Queues each seedable org's content-factory island refresh once per
            # org-local date after the configured local hour. Gated by
            # CONTENT_ISLANDS_SCHEDULER_ENABLED; the dispatch row's unique
            # constraint makes every later tick a cheap existence check.
            ("content_island_refresh", run_island_refresh_scheduler),
            # Sends exact-day seven-day and one-day monthly-update reminders.
            # Feature-gated, scheduled in Melbourne time, and idempotent per
            # recipient/reminder/date so the minute loop cannot double-send.
            ("monthly_update_reminders", run_monthly_update_reminder_scheduler),
            # Releases expired, undispatched MLAI Coding reservations and
            # resolves calls whose provider usage could not be confirmed.
            # Authenticated request paths only reconcile their own account;
            # this production scheduler is the sole global sweep.
            ("coding_reconciliation", reconcile_coding_reservations),
            # Posts one weekday Office Manager callout, closes the volunteer
            # window, repairs message state, and sends the end-of-day reminder.
            ("office_manager", run_office_manager_scheduler),
        ):
            try:
                results[name] = runner()
                if name == "office_manager" and _office_manager_scheduler_failed(
                    results[name]
                ):
                    failures.append(name)
            except Exception as exc:
                logger.exception("Scheduled %s runner failed.", name)
                results[name] = {"status": "failed", "error": str(exc)}
                failures.append(name)

        self.stdout.write(json.dumps(results, sort_keys=True))
        if failures:
            error = f"Scheduled runner(s) failed: {', '.join(failures)}"
            ScheduledDiscoveryHeartbeat.objects.filter(
                name="scheduled_discovery"
            ).update(
                last_failed_at=timezone.now(),
                last_error=error,
                updated_at=timezone.now(),
            )
            raise CommandError(error)
        ScheduledDiscoveryHeartbeat.objects.filter(
            name="scheduled_discovery"
        ).update(
            last_succeeded_at=timezone.now(),
            last_error="",
            updated_at=timezone.now(),
        )
