import json
import logging
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from integrations.management.commands.repair_community_bridge_slack_thread import (
    Command as SingleThreadRepairCommand,
)
from integrations.models import (
    CommunityBridgeChannel,
    CommunityBridgeDelivery,
    CommunityBridgeDeliveryStatus,
    CommunityBridgeDeliveryType,
    CommunityBridgeMessageLink,
    CommunityBridgePlatform,
    CommunityBridgeReceipt,
    CommunityBridgeReceiptStatus,
)
from integrations.services.community_bridge.buzz import BuzzBridgeClient
from integrations.services.community_bridge.slack import SlackBridgeClient


logger = logging.getLogger(__name__)

RECONCILIATION_VERSION = "slack-thread-reconcile-v3"
TERMINAL_STATUSES = {
    CommunityBridgeDeliveryStatus.COMPLETED,
    CommunityBridgeDeliveryStatus.DEAD,
}


class Command(BaseCommand):
    help = (
        "Audit Slack-authoritative thread structure against MLAI Chat and optionally "
        "repair missing, orphaned, incorrectly parented, broadcast, and duplicate events."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--slack-channel-id",
            action="append",
            default=[],
            help="Limit reconciliation to one or more mapped Slack channels.",
        )
        parser.add_argument("--oldest", default="")
        parser.add_argument("--latest", default="")
        parser.add_argument("--max-roots", type=int, default=100)
        parser.add_argument("--maximum-history-messages", type=int, default=10_000)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--confirm-historical-repair", action="store_true")
        parser.add_argument("--wait-seconds", type=int, default=120)
        parser.add_argument(
            "--fail-on-mismatch-rate",
            type=float,
            default=1.0,
            help="Stop apply when mismatched messages divided by scanned messages exceeds this value.",
        )

    def handle(self, *args, **options):
        apply_changes = bool(options["apply"])
        wait_seconds = int(options["wait_seconds"] or 0)
        max_roots = int(options["max_roots"] or 0)
        maximum_history_messages = int(options["maximum_history_messages"] or 0)
        failure_threshold = float(options["fail_on_mismatch_rate"])
        if max_roots < 1 or max_roots > 1000:
            raise CommandError("--max-roots must be between 1 and 1000")
        if maximum_history_messages < 1 or maximum_history_messages > 50_000:
            raise CommandError(
                "--maximum-history-messages must be between 1 and 50000"
            )
        if wait_seconds < 0 or wait_seconds > 300:
            raise CommandError("--wait-seconds must be between 0 and 300")
        if apply_changes and wait_seconds < 1:
            raise CommandError("--wait-seconds must be positive with --apply")
        if apply_changes and not options["confirm_historical_repair"]:
            raise CommandError("--confirm-historical-repair is required with --apply")
        if not 0 <= failure_threshold <= 1:
            raise CommandError("--fail-on-mismatch-rate must be between 0 and 1")
        if not SlackBridgeClient.is_configured():
            raise CommandError("SLACK_BRIDGE_BOT_TOKEN is required")
        if not BuzzBridgeClient.is_configured():
            raise CommandError("The MLAI Chat bridge adapter must be configured")

        channels = CommunityBridgeChannel.objects.filter(
            destination_platform=CommunityBridgePlatform.BUZZ,
            enabled=True,
            sync_replies=True,
            sync_deletes=True,
        ).order_by("slack_channel_id")
        requested_channels = {
            str(channel_id or "").strip()
            for channel_id in options["slack_channel_id"]
            if str(channel_id or "").strip()
        }
        if requested_channels:
            channels = channels.filter(slack_channel_id__in=requested_channels)
        if apply_changes and len(requested_channels) != 1:
            raise CommandError(
                "Apply mode requires exactly one --slack-channel-id for controlled batching"
            )
        if not channels.exists():
            raise CommandError("No enabled MLAI Chat mappings matched the request")

        report = self._empty_report(apply_changes=apply_changes)
        remaining_roots = max_roots
        for channel in channels:
            if remaining_roots <= 0:
                break
            channel_report = self._reconcile_channel(
                channel=channel,
                oldest=str(options["oldest"] or "").strip(),
                latest=str(options["latest"] or "").strip(),
                maximum_history_messages=maximum_history_messages,
                maximum_roots=remaining_roots,
                apply_changes=apply_changes,
                wait_seconds=wait_seconds,
                failure_threshold=failure_threshold,
            )
            report["channels"].append(channel_report)
            remaining_roots -= channel_report["roots_scanned"]
            for key in report["totals"]:
                report["totals"][key] += channel_report.get(key, 0)

        resume_by_channel = {
            item["channel_id"]: item.get("next_latest", "")
            for item in report["channels"]
            if item.get("next_latest")
        }
        report["resume"] = {
            "latest": next(iter(resume_by_channel.values()), "")
            if len(resume_by_channel) == 1
            else "",
            "by_channel": resume_by_channel,
            "version": RECONCILIATION_VERSION,
        }
        logger.info(
            "community_bridge_thread_reconciliation_complete apply=%s channels=%s "
            "roots=%s messages=%s mismatches=%s repaired=%s errors=%s",
            apply_changes,
            len(report["channels"]),
            report["totals"]["roots_scanned"],
            report["totals"]["messages_scanned"],
            report["totals"]["mismatches"],
            report["totals"]["repairs_enqueued"]
            + report["totals"]["links_restored"],
            report["totals"]["errors"],
        )
        self.stdout.write(json.dumps(report, sort_keys=True))

    @staticmethod
    def _empty_report(*, apply_changes):
        counters = {
            "correct": 0,
            "duplicate_events": 0,
            "errors": 0,
            "links_restored": 0,
            "messages_scanned": 0,
            "mismatches": 0,
            "missing_events": 0,
            "orphan_replies": 0,
            "repairs_enqueued": 0,
            "roots_scanned": 0,
            "stale_links": 0,
            "wrong_broadcast": 0,
            "legacy_replay_order": 0,
            "wrong_parent": 0,
        }
        return {"apply": apply_changes, "channels": [], "totals": counters}

    def _reconcile_channel(
        self,
        *,
        channel,
        oldest,
        latest,
        maximum_history_messages,
        maximum_roots,
        apply_changes,
        wait_seconds,
        failure_threshold,
    ):
        report = {
            **self._empty_report(apply_changes=apply_changes)["totals"],
            "channel_id": channel.slack_channel_id,
            "channel_name": channel.slack_channel_name,
            "next_latest": "",
        }
        history = SlackBridgeClient.get_channel_history(
            channel_id=channel.slack_channel_id,
            oldest=oldest,
            latest=latest,
            maximum_messages=maximum_history_messages,
        )
        roots = [
            message
            for message in history
            if self._is_thread_root_summary(message)
        ][:maximum_roots]
        next_latest = (
            self._previous_slack_timestamp(str(roots[-1].get("ts") or ""))
            if roots
            else ""
        )
        # Slack history is newest-first. Applying one bounded page in reverse
        # ensures relay insertion order follows Slack chronology. Operators
        # process pagination pages from oldest to newest as documented by the
        # workflow, so the relay's bounded top-level window finishes with the
        # genuinely newest Slack roots.
        if apply_changes:
            roots = list(reversed(roots))
        for root_summary in roots:
            root_message_id = str(root_summary.get("ts") or "").strip()
            if not root_message_id:
                continue
            report["roots_scanned"] += 1
            report["next_latest"] = next_latest
            try:
                thread = SlackBridgeClient.get_thread_messages(
                    channel_id=channel.slack_channel_id,
                    root_message_id=root_message_id,
                )
                root, replies = SingleThreadRepairCommand._validated_thread(
                    thread, root_message_id=root_message_id
                )
                if apply_changes:
                    audit_report = {
                        **self._empty_report(apply_changes=False)["totals"]
                    }
                    self._reconcile_thread(
                        channel=channel,
                        root=root,
                        replies=replies,
                        report=audit_report,
                        apply_changes=False,
                        wait_seconds=wait_seconds,
                    )
                    projected_mismatches = (
                        report["mismatches"] + audit_report["mismatches"]
                    )
                    projected_messages = (
                        report["messages_scanned"]
                        + audit_report["messages_scanned"]
                    )
                    mismatch_rate = projected_mismatches / max(
                        1, projected_messages
                    )
                    if mismatch_rate > failure_threshold:
                        raise CommandError(
                            f"Mismatch rate {mismatch_rate:.3f} exceeded configured threshold"
                        )
                self._reconcile_thread(
                    channel=channel,
                    root=root,
                    replies=replies,
                    report=report,
                    apply_changes=apply_changes,
                    wait_seconds=wait_seconds,
                )
            except Exception as exc:
                report["errors"] += 1
                logger.exception(
                    "community_bridge_thread_reconciliation_failed channel=%s root=%s",
                    channel.slack_channel_id,
                    root_message_id,
                )
                if apply_changes:
                    raise CommandError(
                        f"Reconciliation failed for {channel.slack_channel_id}:{root_message_id}: "
                        f"{exc.__class__.__name__}"
                    ) from exc
        return report

    @staticmethod
    def _is_thread_root_summary(message):
        """Return whether a Slack history item is a root with replies.

        Slack may omit ``thread_ts`` on a root or set it to the root's own
        ``ts``. Only a different ``thread_ts`` identifies a reply.
        """

        message_id = str(message.get("ts") or "").strip()
        thread_id = str(message.get("thread_ts") or "").strip()
        return bool(
            message_id
            and int(message.get("reply_count") or 0) > 0
            and (not thread_id or thread_id == message_id)
        )

    @staticmethod
    def _previous_slack_timestamp(value):
        """Return the immediately preceding timestamp for inclusive Slack pagination."""

        normalized = str(value or "").strip()
        try:
            seconds, fraction = normalized.split(".", 1)
            if not seconds.isdigit() or not fraction.isdigit():
                return ""
            scale = 10 ** len(fraction)
            combined = int(seconds) * scale + int(fraction)
            if combined <= 0:
                return ""
            previous = combined - 1
            return f"{previous // scale}.{previous % scale:0{len(fraction)}d}"
        except (TypeError, ValueError):
            return ""

    def _reconcile_thread(
        self,
        *,
        channel,
        root,
        replies,
        report,
        apply_changes,
        wait_seconds,
    ):
        messages = [root, *replies]
        links = {
            link.source_message_id: link
            for link in CommunityBridgeMessageLink.objects.filter(
                channel=channel,
                source_platform=CommunityBridgePlatform.SLACK,
                source_message_id__in=[message["ts"] for message in messages],
                destination_platform=CommunityBridgePlatform.BUZZ,
            )
        }
        matches_by_source = self._lookup_matches(
            channel=channel,
            messages=messages,
            links=links,
        )
        root_message_id = str(root["ts"])
        root_destination_id = self._reconcile_message(
            channel=channel,
            message=root,
            root_message_id=root_message_id,
            expected_parent_id="",
            link=links.get(root_message_id),
            matches=matches_by_source.get(root_message_id, []),
            report=report,
            apply_changes=apply_changes,
            wait_seconds=wait_seconds,
        )
        if apply_changes and not root_destination_id:
            raise CommandError("Root reconciliation completed without a destination mapping")
        expected_reply_parent_id = (
            root_destination_id or f"missing-root:{root_message_id}"
        )
        for reply in replies:
            reply_id = str(reply["ts"])
            self._reconcile_message(
                channel=channel,
                message=reply,
                root_message_id=root_message_id,
                expected_parent_id=expected_reply_parent_id,
                link=links.get(reply_id),
                matches=matches_by_source.get(reply_id, []),
                report=report,
                apply_changes=apply_changes,
                wait_seconds=wait_seconds,
            )

    @staticmethod
    def _lookup_matches(*, channel, messages, links):
        matches = []
        message_ids = [str(message["ts"]) for message in messages]
        for start in range(0, len(message_ids), 100):
            chunk = message_ids[start : start + 100]
            destination_ids = [
                links[message_id].destination_message_id
                for message_id in chunk
                if message_id in links
                and str(links[message_id].destination_message_id or "").strip()
            ]
            matches.extend(
                BuzzBridgeClient.lookup_messages(
                    channel_id=channel.destination_channel_id,
                    source_workspace_id=channel.slack_workspace_id,
                    source_channel_id=channel.slack_channel_id,
                    source_message_ids=chunk,
                    destination_message_ids=destination_ids,
                )
            )
        grouped = defaultdict(list)
        for match in matches:
            grouped[match["source_message_id"]].append(match)
        return grouped

    def _reconcile_message(
        self,
        *,
        channel,
        message,
        root_message_id,
        expected_parent_id,
        link,
        matches,
        report,
        apply_changes,
        wait_seconds,
    ):
        report["messages_scanned"] += 1
        message_id = str(message["ts"])
        expected_broadcast = bool(expected_parent_id) and (
            str(message.get("subtype") or "").strip() == "thread_broadcast"
            or bool(message.get("reply_broadcast"))
        )
        active_link = bool(
            link
            and link.source_deleted_at is None
            and link.destination_deleted_at is None
        )
        link_destination_id = (
            str(link.destination_message_id or "").strip() if active_link else ""
        )
        link_repair_version = str(
            ((link.source_payload or {}).get("metadata") or {}).get(
                "backfill_version"
            )
            if link
            else ""
        ).strip()
        requires_chronological_replay = bool(
            active_link and link_repair_version != RECONCILIATION_VERSION
        )
        if requires_chronological_replay:
            report["legacy_replay_order"] += 1
        structurally_correct_matches = [
            match
            for match in matches
            if match["parent_message_id"] == expected_parent_id
            and bool(match["broadcast"]) == expected_broadcast
        ]
        chosen = None
        if not requires_chronological_replay:
            chosen = next(
                (
                    match
                    for match in structurally_correct_matches
                    if match["destination_message_id"] == link_destination_id
                ),
                structurally_correct_matches[0]
                if structurally_correct_matches
                else None,
            )
        duplicate_matches = [match for match in matches if chosen and match != chosen]
        message_mismatch = bool(duplicate_matches or not chosen)
        if duplicate_matches:
            report["duplicate_events"] += len(duplicate_matches)
        if chosen:
            database_matches = bool(
                active_link
                and link_destination_id == chosen["destination_message_id"]
                and str(link.destination_parent_message_id or "").strip()
                == expected_parent_id
            )
            if database_matches and not duplicate_matches:
                report["correct"] += 1
            elif not database_matches:
                message_mismatch = True
                report["links_restored"] += 1
                if apply_changes:
                    link = self._restore_link(
                        channel=channel,
                        message=message,
                        root_message_id=root_message_id,
                        match=chosen,
                    )
        else:
            if not matches:
                report["missing_events"] += 1
                if active_link:
                    report["stale_links"] += 1
            else:
                if expected_parent_id and any(
                    not match["parent_message_id"] for match in matches
                ):
                    report["orphan_replies"] += 1
                if any(
                    match["parent_message_id"] != expected_parent_id
                    for match in matches
                ):
                    report["wrong_parent"] += 1
                if any(
                    bool(match["broadcast"]) != expected_broadcast
                    for match in matches
                ):
                    report["wrong_broadcast"] += 1
        if message_mismatch:
            report["mismatches"] += 1

        if not apply_changes:
            return chosen["destination_message_id"] if chosen else ""

        for duplicate in duplicate_matches:
            self._enqueue_override_delete(
                channel=channel,
                message=message,
                root_message_id=root_message_id,
                destination_message_id=duplicate["destination_message_id"],
                wait_seconds=wait_seconds,
            )
            report["repairs_enqueued"] += 1
        if chosen:
            return chosen["destination_message_id"]

        for match in matches:
            self._enqueue_override_delete(
                channel=channel,
                message=message,
                root_message_id=root_message_id,
                destination_message_id=match["destination_message_id"],
                wait_seconds=wait_seconds,
            )
            report["repairs_enqueued"] += 1
        if link:
            now = timezone.now()
            CommunityBridgeMessageLink.objects.filter(id=link.id).update(
                destination_deleted_at=now,
            )
        delivery, _ = SingleThreadRepairCommand._enqueue(
            channel=channel,
            message=message,
            root_message_id=root_message_id,
            delivery_type=CommunityBridgeDeliveryType.CREATE,
            receipt_suffix="create",
            repair_version=RECONCILIATION_VERSION,
        )
        self._wait(delivery=delivery, wait_seconds=wait_seconds)
        report["repairs_enqueued"] += 1
        refreshed = CommunityBridgeMessageLink.objects.filter(
            channel=channel,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id=message_id,
            destination_platform=CommunityBridgePlatform.BUZZ,
            source_deleted_at__isnull=True,
            destination_deleted_at__isnull=True,
        ).first()
        return str(getattr(refreshed, "destination_message_id", "") or "").strip()

    @staticmethod
    def _restore_link(*, channel, message, root_message_id, match):
        message_id = str(message["ts"])
        parent_message_id = root_message_id if message_id != root_message_id else ""
        payload = SingleThreadRepairCommand._payload(
            message=message,
            channel=channel,
            receipt_key=f"{RECONCILIATION_VERSION}:restore:{channel.slack_channel_id}:{message_id}",
            parent_message_id=parent_message_id,
            delivery_type=CommunityBridgeDeliveryType.CREATE,
            repair_version=RECONCILIATION_VERSION,
        )
        link, _ = CommunityBridgeMessageLink.objects.update_or_create(
            source_platform=CommunityBridgePlatform.SLACK,
            source_channel_id=channel.slack_channel_id,
            source_message_id=message_id,
            destination_platform=CommunityBridgePlatform.BUZZ,
            defaults={
                "channel": channel,
                "source_parent_message_id": parent_message_id,
                "source_author_id": str(message.get("user") or "").strip(),
                "destination_channel_id": channel.destination_channel_id,
                "destination_message_id": match["destination_message_id"],
                "destination_parent_message_id": match["parent_message_id"],
                "source_payload": payload,
                "destination_payload": {
                    "broadcast": bool(match["broadcast"]),
                    "reconciled_by": RECONCILIATION_VERSION,
                },
                "source_deleted_at": None,
                "destination_deleted_at": None,
            },
        )
        return link

    @staticmethod
    def _enqueue_override_delete(
        *, channel, message, root_message_id, destination_message_id, wait_seconds
    ):
        message_id = str(message["ts"])
        receipt_key = (
            f"{RECONCILIATION_VERSION}:delete:{channel.slack_channel_id}:"
            f"{message_id}:{destination_message_id}"
        )
        parent_message_id = root_message_id if message_id != root_message_id else ""
        payload = SingleThreadRepairCommand._payload(
            message=message,
            channel=channel,
            receipt_key=receipt_key,
            parent_message_id=parent_message_id,
            delivery_type=CommunityBridgeDeliveryType.DELETE,
            repair_version=RECONCILIATION_VERSION,
        )
        payload["metadata"]["destination_message_id_override"] = destination_message_id
        now = timezone.now()
        with transaction.atomic():
            receipt, created = CommunityBridgeReceipt.objects.get_or_create(
                platform=CommunityBridgePlatform.SLACK,
                receipt_key=receipt_key,
                defaults={
                    "channel": channel,
                    "event_type": RECONCILIATION_VERSION,
                    "source_channel_id": channel.slack_channel_id,
                    "source_message_id": message_id,
                    "source_parent_message_id": parent_message_id,
                    "status": CommunityBridgeReceiptStatus.ENQUEUED,
                    "queued_delivery_count": 1,
                    "payload": {},
                    "processed_at": now,
                },
            )
            if created:
                delivery = CommunityBridgeDelivery.objects.create(
                    channel=channel,
                    receipt=receipt,
                    target_platform=CommunityBridgePlatform.BUZZ,
                    source_platform=CommunityBridgePlatform.SLACK,
                    delivery_type=CommunityBridgeDeliveryType.DELETE,
                    status=CommunityBridgeDeliveryStatus.PENDING,
                    source_event_key=receipt_key,
                    source_channel_id=channel.slack_channel_id,
                    source_message_id=message_id,
                    source_parent_message_id=parent_message_id,
                    target_channel_id=channel.destination_channel_id,
                    payload=payload,
                    available_at=now,
                )
            else:
                delivery = receipt.deliveries.order_by("id").first()
        Command._wait(delivery=delivery, wait_seconds=wait_seconds)

    @staticmethod
    def _wait(*, delivery, wait_seconds):
        if not delivery:
            raise CommandError("Reconciliation receipt has no delivery")
        if delivery.status == CommunityBridgeDeliveryStatus.DEAD:
            raise CommandError(f"Reconciliation delivery {delivery.id} is dead")
        if delivery.status in TERMINAL_STATUSES:
            return
        try:
            SingleThreadRepairCommand._wait_for_deliveries(
                delivery_ids=[delivery.id], wait_seconds=wait_seconds
            )
        except CommandError as exc:
            delivery.refresh_from_db(fields=["status", "last_error"])
            detail = str(delivery.last_error or "").strip()
            raise CommandError(
                f"Reconciliation delivery {delivery.id} failed"
                + (f": {detail}" if detail else "")
            ) from exc
