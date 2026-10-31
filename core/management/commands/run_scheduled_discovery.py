import json
import logging
from datetime import date as calendar_date

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.scheduling import run_runners
from core.slack_founder_link_retention import (
    run_scheduled_slack_founder_link_request_cleanup,
)
from content_analytics.services.report_scheduler import run_daily_article_report_scheduler
from content_factory.founder_journey_alerts import run_founder_journey_alert_sweep
from content_factory.founder_journey_sweeps import scheduled_founder_journey_sweeps
from content_factory.reconciliation import run_content_factory_reconciliation_sweep
from content_factory.website_reconciliation import process_website_connection_operations
from content_factory.dispatch_outbox import process_dispatch_outbox
from content_factory.services.island_refresh_scheduler import run_island_refresh_scheduler
from crm.blasts import run_blast_dispatch_scheduler
from crm.listmonk_sync import run_listmonk_sync_scheduler
from crm.luma_sync import run_luma_guest_sync_scheduler
from crm.warmup import run_warmup_scheduler
from integrations.services.daily_discovery import (
    enqueue_scheduled_discovery,
    run_daily_discovery_scheduler,
)
from integrations.services.github_installations import (
    run_github_installation_reconciliation_sweep,
)
from integrations.services.research_automations import run_research_automation_scheduler
from integrations.services.xero_reconciliation import run_daily_payout_reconciliation
from jobs.services.job_pipeline import enqueue_daily_jobs as run_daily_jobs_scheduler
from hospital.sim_retention import run_scheduled_sim_conversation_cleanup
from roo.coding import reconcile_coding_reservations
from roo.models import ScheduledDiscoveryHeartbeat
from startup_updates.monthly_update_reminders import run_monthly_update_reminder_scheduler

from content_factory.website_consistency import run_website_consistency_sweep

logger = logging.getLogger(__name__)


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

        results, failures = run_runners((
            ("daily_discovery", run_daily_discovery_scheduler),
            ("jobs", run_daily_jobs_scheduler),
            # Drives the daily research-topic email/Slack/WhatsApp send (8am slot).
            # Idempotent per run, so it is safe to tick every scheduler loop.
            ("research_automations", run_research_automation_scheduler),
            # Closes runs whose content-factory callbacks were permanently
            # lost (outbox retries exhaust after ~2h) and dispatch ghosts.
            # Self-throttling per run, so it is safe to tick every loop.
            ("content_factory_reconciliation", run_content_factory_reconciliation_sweep),
            # Tells ops once per stuck founder (run stopped or blocked, no
            # draft 24 h after a startup is created, Your turn for 72 h), for
            # every org. Gated by ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED and
            # bounded by ARTICLES_FOUNDER_JOURNEY_ALERTS_MAX_PER_TICK.
            ("founder_journey_alerts", run_founder_journey_alert_sweep),
            # P1 founder-journey sweeps (first profile, draft-ready outbox).
            # Runs only while MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED is on;
            # each sweep checks its own flag and never raises.
            ("founder_journey_sweeps", scheduled_founder_journey_sweeps),
            # Prunes stale GitHub App installations (founder uninstalled the
            # App) from the founder registry so dead rows stop poisoning the
            # "registry exists" guards. Self-throttling (min-age + probe
            # interval + batch cap), so it is safe to tick every loop.
            ("github_installation_reconciliation", run_github_installation_reconciliation_sweep),
            ("website_connection_operations", process_website_connection_operations),
            ("website_consistency", run_website_consistency_sweep),
            ("article_dispatch_outbox", process_dispatch_outbox),
            # Refreshes the durable Stripe payout ledger once per local day.
            # This never posts to Xero; posting always requires admin approval.
            ("stripe_payout_reconciliation", run_daily_payout_reconciliation),
            # Enforces the configured raw-dialogue retention window once per
            # local day. Its cache marker makes the minute scheduler tick cheap.
            ("health_hack_conversation_retention", run_scheduled_sim_conversation_cleanup),
            # Bounds terminal Roo-Founder link-request storage. The runner is
            # cache-locked and executes once per Melbourne-local day.
            (
                "slack_founder_link_request_retention",
                run_scheduled_slack_founder_link_request_cleanup,
            ),
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
            # Copies MLAI Luma guests into crm contacts. Feature-gated,
            # cache-throttled to one pass per CRM_LUMA_SYNC_INTERVAL_SECONDS,
            # and paced under the calendar key's shared rate limit. A pass
            # starts no new event after crm.luma_sync.PASS_SECONDS and leaves
            # the rest to the next tick, so it cannot stall this one.
            ("crm_luma_guest_sync", run_luma_guest_sync_scheduler),
            # Sends queued crm blasts: email through listmonk, SMS and WhatsApp
            # through Twilio. Off unless a blast channel is enabled, and
            # cache-throttled to CRM_BLAST_DISPATCH_INTERVAL_SECONDS.
            ("crm_warmup", run_warmup_scheduler),
            ("crm_blast_dispatch", run_blast_dispatch_scheduler),
            # Polls listmonk for campaign progress, opens, clicks, bounces and
            # unsubscribes. Runs whenever listmonk is configured, throttled to
            # CRM_LISTMONK_SYNC_INTERVAL_SECONDS (about five minutes).
            ("crm_listmonk_sync", run_listmonk_sync_scheduler),
            # Releases expired, undispatched MLAI Coding reservations and
            # resolves calls whose provider usage could not be confirmed.
            # Authenticated request paths only reconcile their own account;
            # this production scheduler is the sole global sweep.
            ("coding_reconciliation", lambda: {"status": "completed", **reconcile_coding_reservations()}),
        ))
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
