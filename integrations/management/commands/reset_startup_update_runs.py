from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.models import (
    ContentFactoryRun,
    ContentFactoryRunStatus,
    ContentFactoryRunStepAttempt,
    ContentFactoryStepStatus,
)
from integrations.services.startup_updates import OPEN_RUN_STATUSES, STARTUP_UPDATE_WORKFLOW


LOCAL_RESET_ERROR = "Locally reset stale startup-update run."
LOCAL_RESET_STEP_MESSAGE = "Run reset for local recovery."


def _clear_valley_meta(result: dict | None) -> dict:
    payload = dict(result or {})
    meta = dict(payload.get("_valley_meta") or {})
    meta["lease_owner"] = None
    meta["lease_expires_at"] = None
    meta["last_heartbeat_at"] = None
    meta["last_error"] = ""
    meta["dead_letters"] = []
    payload["_valley_meta"] = meta
    return payload


def _reset_run_state(run: ContentFactoryRun, *, now) -> None:
    run.status = ContentFactoryRunStatus.FAILED
    run.error = LOCAL_RESET_ERROR
    run.resume_available = False
    run.result = _clear_valley_meta(run.result)
    run.save(update_fields=["status", "error", "resume_available", "result", "updated_at"])

    active_steps = run.steps.filter(
        status__in=[
            ContentFactoryStepStatus.RUNNING,
            ContentFactoryStepStatus.BLOCKED,
        ]
    )
    for step in active_steps:
        step.status = ContentFactoryStepStatus.FAILED
        step.message = LOCAL_RESET_STEP_MESSAGE
        step.error = LOCAL_RESET_ERROR
        if not step.completed_at:
            step.completed_at = now
        step.save(update_fields=["status", "message", "error", "completed_at"])

    active_attempts = ContentFactoryRunStepAttempt.objects.filter(
        step__run=run,
        completed_at__isnull=True,
        status__in=[
            ContentFactoryStepStatus.RUNNING,
            ContentFactoryStepStatus.BLOCKED,
        ],
    )
    for attempt in active_attempts:
        attempt.status = ContentFactoryStepStatus.FAILED
        attempt.message = LOCAL_RESET_STEP_MESSAGE
        attempt.error = LOCAL_RESET_ERROR
        attempt.completed_at = now
        attempt.save(update_fields=["status", "message", "error", "completed_at"])


class Command(BaseCommand):
    help = "Dry-run or reset stale/open startup-update runs for local development."

    def add_arguments(self, parser):
        parser.add_argument(
            "--run-id",
            action="append",
            dest="run_ids",
            default=[],
            help="Target a specific startup-update run id. May be passed multiple times.",
        )
        parser.add_argument(
            "--domain",
            type=str,
            default="",
            help="Limit matches to runs for a single organization domain.",
        )
        parser.add_argument(
            "--older-than-minutes",
            type=int,
            default=None,
            help="Only match runs whose updated_at is older than this many minutes.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply the reset. Without this flag the command only prints matching runs.",
        )

    def handle(self, *args, **options):
        older_than_minutes = options["older_than_minutes"]
        if older_than_minutes is not None and older_than_minutes < 0:
            raise CommandError("--older-than-minutes must be >= 0.")

        queryset = ContentFactoryRun.objects.filter(
            workflow=STARTUP_UPDATE_WORKFLOW,
            status__in=list(OPEN_RUN_STATUSES),
        ).order_by("-updated_at")
        domain = str(options.get("domain") or "").strip()
        run_ids = [str(item).strip() for item in options.get("run_ids") or [] if str(item).strip()]
        if domain:
            queryset = queryset.filter(domain=domain)
        if run_ids:
            queryset = queryset.filter(run_id__in=run_ids)
        if older_than_minutes is not None:
            cutoff = timezone.now() - timedelta(minutes=older_than_minutes)
            queryset = queryset.filter(updated_at__lte=cutoff)

        runs = list(queryset)
        if not runs:
            self.stdout.write("No matching open startup-update runs found.")
            return

        action = "Applying reset" if options["apply"] else "Dry run"
        self.stdout.write(f"{action} for {len(runs)} startup-update run(s):")
        for run in runs:
            self.stdout.write(
                f"- {run.run_id} domain={run.domain or '-'} status={run.status} "
                f"step={run.current_step or '-'} updated_at={run.updated_at.isoformat()}"
            )

        if not options["apply"]:
            self.stdout.write("Re-run with --apply to mark these runs failed for local recovery.")
            return

        now = timezone.now()
        with transaction.atomic():
            for run in runs:
                _reset_run_state(run, now=now)

        self.stdout.write(self.style.SUCCESS(f"Reset {len(runs)} startup-update run(s)."))
