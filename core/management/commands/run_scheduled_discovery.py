import json
import logging
from datetime import date as calendar_date

from django.core.management.base import BaseCommand, CommandError

from content_analytics.services.report_scheduler import run_daily_article_report_scheduler
from content_factory.reconciliation import run_content_factory_reconciliation_sweep
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
        ):
            try:
                results[name] = runner()
            except Exception as exc:
                logger.exception("Scheduled %s runner failed.", name)
                results[name] = {"status": "failed", "error": str(exc)}
                failures.append(name)

        self.stdout.write(json.dumps(results, sort_keys=True))
        if failures:
            raise CommandError(f"Scheduled runner(s) failed: {', '.join(failures)}")
