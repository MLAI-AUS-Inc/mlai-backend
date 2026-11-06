"""Plan or schedule bounded repair through the normal member-authorized worker."""
import json
import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from integrations.models import BridgeSyncJob, BridgeSyncState, CommunityBridgeChannel
from integrations.services.community_bridge.member_channels import member_channel_in_scope
from integrations.services.message_sync.scheduler import schedule_job


class Command(BaseCommand):
    help = "Plan a public room's durable member repair; --apply schedules work without reading Slack or replaying messages."

    def add_arguments(self, parser):
        parser.add_argument("--workspace-id", required=True)
        parser.add_argument("--channel-id", required=True)
        parser.add_argument("--thread-ts", action="append", default=[],
                            help="An exact Slack thread root timestamp; repeat up to 20 times.")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        workspace, source = options["workspace_id"], options["channel_id"]
        threads = options["thread_ts"]
        if (not re.fullmatch(r"T[A-Z0-9]+", workspace)
                or not re.fullmatch(r"C[A-Z0-9]+", source)):
            raise CommandError("Supply exact Slack workspace and public channel IDs.")
        if len(threads) > 20 or any(not re.fullmatch(r"[0-9]{1,12}\.[0-9]{6}", ts) for ts in threads):
            raise CommandError("Supply at most 20 exact Slack thread timestamps.")
        mapping = CommunityBridgeChannel.objects.filter(slack_workspace_id=workspace,
            slack_channel_id=source, destination_platform="buzz").first()
        self._validate_mapping(mapping)
        enabled = bool(getattr(settings, "MESSAGE_SYNC_ENABLED", False))
        if options["apply"] and not enabled:
            raise CommandError("Message sync is paused; repair cannot bypass the processing pause.")
        wanted = [("head", ""), ("archive", "")] + [("thread", ts) for ts in sorted(set(threads))]
        report = {"mode": "apply" if options["apply"] else "dry_run", "enabled": enabled,
                  "workspace_id": workspace, "channel_id": source, "room_id": mapping.destination_channel_id,
                  "read_authority": "worker_revalidates_current_member", "history_days": 30,
                  "provider_calls": 0, "jobs": []}
        if not options["apply"]:
            state = BridgeSyncState.objects.filter(public_channel=mapping).first()
            jobs = {(job.kind, job.source_object_key): job for job in state.jobs.all()} if state else {}
            for kind, key in wanted:
                report["jobs"].append(self._summary(jobs.get((kind, key)), kind, key, "plan"))
        else:
            with transaction.atomic():
                # State precedes Channel, as in history and membership invalidation.
                state, _ = BridgeSyncState.objects.get_or_create(public_channel=mapping,
                    defaults={"workspace_id": workspace, "source_channel_id": source})
                state = BridgeSyncState.objects.select_for_update().get(pk=state.pk)
                current = CommunityBridgeChannel.objects.select_for_update().get(pk=mapping.pk)
                self._validate_mapping(current)
                if (self._identity(current) != self._identity(mapping)
                        or state.workspace_id != workspace or state.source_channel_id != source):
                    raise CommandError("The mapping changed; inspect it before scheduling repair.")
                if state.status in {"paused", "revoked"}:
                    raise CommandError("The room is paused or revoked; repair cannot reopen it.")
                now = timezone.now()
                for kind, key in wanted:
                    old = BridgeSyncJob.objects.select_for_update().filter(
                        state=state, kind=kind, source_object_key=key).first()
                    job = old or schedule_job(state, kind, source_object_key=key)
                    action = "created" if old is None else "preserved"
                    # A completed, idle scan may start its normal next pass.
                    # Never reset a checkpoint, steal a lease or shorten a backoff.
                    if (old is not None and kind != "archive" and job.completed_at is not None
                            and not job.checkpoint and not job.last_error_code and not job.backoff_seconds
                            and (job.lease_expires_at is None or job.lease_expires_at <= now)
                            and job.due_at > now):
                        job.due_at = now
                        job.save(update_fields=("due_at",))
                        action = "scheduled_next_pass"
                    report["jobs"].append(self._summary(job, kind, key, action))
        self.stdout.write(json.dumps(report, sort_keys=True))

    @staticmethod
    def _validate_mapping(row):
        if (row is None or not row.enabled or row.audience not in {"community", "members"}
                or not row.destination_channel_id or row.slack_visibility != "public"
                or row.slack_archived or row.slack_deleted or row.mirror_decision != "mirror"
                or row.hold_reason or not member_channel_in_scope(row.slack_workspace_id, row.slack_channel_id)):
            raise CommandError("An admitted public MLAI Chat mapping is required.")

    @staticmethod
    def _identity(row):
        return (row.slack_workspace_id, row.slack_channel_id, row.destination_workspace_id,
                row.destination_channel_id, row.audience)

    @staticmethod
    def _summary(job, kind, key, action):
        return {"kind": kind, "thread_ts": key, "action": action,
                "exists": job is not None,
                "due_at": job.due_at.isoformat() if job else None,
                "checkpoint_retained": bool(job and job.checkpoint),
                "active_lease": bool(job and job.lease_expires_at and job.lease_expires_at > timezone.now()),
                "backoff_seconds": job.backoff_seconds if job else 0}
