import json
import time
from io import StringIO

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from integrations.models import CommunityBridgeChannel, CommunityBridgePlatform


class Command(BaseCommand):
    help = (
        "Repair recent Slack-to-MLAI Chat gaps across every enabled mapping. "
        "Slack Events remains the real-time path; this bounded pass is its safety net."
    )

    def add_arguments(self, parser):
        parser.add_argument("--lookback-seconds", type=int, default=24 * 60 * 60)
        parser.add_argument("--max-roots-per-channel", type=int, default=100)
        parser.add_argument("--maximum-history-messages", type=int, default=1000)
        parser.add_argument("--wait-seconds", type=int, default=60)

    def handle(self, *args, **options):
        lookback_seconds = int(options["lookback_seconds"])
        max_roots = int(options["max_roots_per_channel"])
        maximum_history_messages = int(options["maximum_history_messages"])
        wait_seconds = int(options["wait_seconds"])
        if not 60 <= lookback_seconds <= 7 * 24 * 60 * 60:
            raise CommandError("--lookback-seconds must be between 60 and 604800")
        if not 1 <= max_roots <= 250:
            raise CommandError("--max-roots-per-channel must be between 1 and 250")
        if not 1 <= maximum_history_messages <= 5000:
            raise CommandError("--maximum-history-messages must be between 1 and 5000")
        if not 1 <= wait_seconds <= 300:
            raise CommandError("--wait-seconds must be between 1 and 300")

        channel_ids = list(
            CommunityBridgeChannel.objects.filter(
                destination_platform=CommunityBridgePlatform.BUZZ,
                enabled=True,
                sync_replies=True,
                sync_deletes=True,
            )
            .order_by("slack_channel_id")
            .values_list("slack_channel_id", flat=True)
        )
        if not channel_ids:
            self.stdout.write(
                json.dumps({"channels": 0, "errors": 0, "mismatches": 0, "repairs": 0})
            )
            return

        oldest = f"{max(1, int(time.time()) - lookback_seconds)}.000000"
        totals = {"channels": 0, "errors": 0, "mismatches": 0, "repairs": 0}
        for channel_id in channel_ids:
            output = StringIO()
            try:
                call_command(
                    "reconcile_community_bridge_slack_threads",
                    slack_channel_id=[channel_id],
                    oldest=oldest,
                    latest="",
                    max_roots=max_roots,
                    maximum_history_messages=maximum_history_messages,
                    include_unthreaded=True,
                    apply=True,
                    confirm_historical_repair=True,
                    wait_seconds=wait_seconds,
                    fail_on_mismatch_rate=1.0,
                    stdout=output,
                )
                report = json.loads(output.getvalue().strip().splitlines()[-1])
                report_totals = dict(report.get("totals") or {})
                totals["channels"] += 1
                totals["errors"] += int(report_totals.get("errors") or 0)
                totals["mismatches"] += int(report_totals.get("mismatches") or 0)
                totals["repairs"] += int(report_totals.get("repairs_enqueued") or 0)
                totals["repairs"] += int(report_totals.get("links_restored") or 0)
            except Exception as exc:
                totals["errors"] += 1
                self.stderr.write(
                    f"Recent bridge reconciliation failed for {channel_id}: "
                    f"{exc.__class__.__name__}"
                )
        self.stdout.write(json.dumps(totals, sort_keys=True))
        if totals["errors"]:
            raise CommandError(
                f"Recent bridge reconciliation completed with {totals['errors']} errors"
            )
