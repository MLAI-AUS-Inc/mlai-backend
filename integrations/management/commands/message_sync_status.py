"""Content-free diagnostics and worker liveness for the durable sync lanes."""
import json
import socket
import math
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Max, Min, FloatField
from django.db.models.functions import Cast
from django.utils import timezone
from integrations.models import (
    BridgeApiBudget, BridgeSyncInbox, BridgeSyncJob, BridgeSyncState,
    BridgeWorkerHeartbeat, CommunityBridgeDelivery, SlackDmMirrorDelivery, SlackDmMirrorGrant,
)
from integrations.services.message_sync import telemetry
from integrations.services.message_sync import event_routing
from integrations.services.message_sync.scheduler import eligible_states, safe_error_code
from integrations.services.message_sync.discovery import KEY as DISCOVERY_KEY, due_discovery_grants
from integrations.services.slack_open_requests import KEY as OPEN_KEY
from integrations.services.message_sync.slack_client import provider_interval
from integrations.services.community_bridge import community_source
from integrations.services.community_bridge.community_source_codec import stamp
from django.utils.dateparse import parse_datetime


class Command(BaseCommand):
    help = "Show sync backlog, provider cooldowns, coverage and worker health without message content."

    def add_arguments(self, parser):
        parser.add_argument('--check', action='store_true', help='Fail if an enabled worker lane is stale or absent.')
        parser.add_argument('--local-worker', action='store_true', help='Check only this container, not a previous deployment.')
        parser.add_argument('--max-heartbeat-age', type=int, default=180)
        parser.add_argument('--window-minutes', type=int, default=5,
                            help='Completed minute buckets for provider throughput (1–15).')
        parser.add_argument('--window-hours', type=int, default=None,
                            help='Use retained completed hour buckets instead (1–168).')

        parser.add_argument("--community-source-cutoff", default=None,
                            help="Explicit aware UTC cutoff for Community source observations.")
        parser.add_argument("--finalize-community-source", action="store_true",
                            help="Save a versioned source report receipt; changes no rollout flag.")

    def handle(self, *args, **options):
        enabled = bool(getattr(settings, 'MESSAGE_SYNC_ENABLED', False))
        if options['max_heartbeat_age'] < 1:
            raise CommandError('--max-heartbeat-age must be positive')
        if not 1 <= options['window_minutes'] <= 15:
            raise CommandError('--window-minutes must be between 1 and 15')
        if options['window_hours'] is not None and not 1 <= options['window_hours'] <= 168:
            raise CommandError('--window-hours must be between 1 and 168')
        if options['check'] and not enabled:
            self.stdout.write(json.dumps({'enabled': False}))
            return
        cutoff_ns = None
        if options["community_source_cutoff"] is not None:
            try:
                cutoff = parse_datetime(options["community_source_cutoff"])
                cutoff_ns = stamp(cutoff)
            except (TypeError, ValueError, OverflowError):
                raise CommandError("--community-source-cutoff requires an aware ISO timestamp") from None
        if options["finalize_community_source"] and cutoff_ns is None:
            raise CommandError("--finalize-community-source requires an explicit cutoff")
        now = timezone.now()
        workers = BridgeWorkerHeartbeat.objects.all()
        if options['local_worker']:
            workers = workers.filter(worker_id__startswith=socket.gethostname() + ':')
        lane_times = dict(workers.values('lane').annotate(latest=Max('heartbeat_at')).values_list('lane', 'latest'))
        stale = [lane for lane in ('inbox', 'history', 'public_delivery', 'private_delivery', 'read_state')
                 if lane not in lane_times or lane_times[lane] < now - timedelta(seconds=options['max_heartbeat_age'])]
        if options['check']:
            self.stdout.write(json.dumps({'enabled': enabled, 'stale_lanes': stale}))
            if stale:
                raise CommandError('Durable sync worker lanes are not healthy: ' + ', '.join(stale))
            return

        def age(value):
            return max(0, int((now - value).total_seconds())) if value else 0

        def delivery(query):
            pending = query.exclude(status='completed')
            return {
                'by_status': list(pending.values('status').annotate(count=Count('id')).order_by('status')),
                'oldest_pending_age_seconds': age(pending.aggregate(value=Min('created_at'))['value']),
            }

        inbox = BridgeSyncInbox.objects.exclude(status='completed')
        due = BridgeSyncJob.objects.filter(due_at__lte=now, state__in=eligible_states())
        budgets = list(BridgeApiBudget.objects.filter(method__in=telemetry.METHODS))
        scopes = {telemetry.scope_key(b.app_id, b.workspace_id, b.method): b for b in budgets}
        try:
            hours = options['window_hours']
            throughput = (telemetry.hourly_snapshot(scopes, hours=hours) if hours is not None
                          else telemetry.snapshot(scopes, minutes=options['window_minutes']))
            minutes = hours * 60 if hours is not None else options['window_minutes']
            rows = []
            for scope, counters in throughput.pop('scopes').items():
                if counters is None:
                    continue
                budget = scopes[scope]
                allowance = 60 / provider_interval(budget.method)
                complete_window = (throughput['observed_hours'][scope] == hours if hours is not None
                                   else throughput['observed_minutes'][scope] == minutes)
                rows.append({'scope': scope, 'method': budget.method, **counters,
                             'requests_per_minute': round(counters['admitted'] / minutes, 2) if complete_window else None,
                             'configured_requests_per_minute': allowance,
                             'configured_budget_used_percent': round(100 * counters['admitted'] / (minutes * allowance), 1) if complete_window else None,
                             'mean_request_ms': round(counters['request_ms'] / counters['finished']) if counters['finished'] else None})
            throughput.update(available=True, measured_scopes=rows,
                              reliability='best_effort_lower_bound; observed buckets do not prove all requests were recorded')
        except Exception:
            throughput = {'available': False}
        active_grants = SlackDmMirrorGrant.objects.filter(status="active", revoked_at__isnull=True, connection__isnull=False)
        discovery, opens = summarize_open_discovery(active_grants.values_list("connection__sync_cursor", flat=True), now=now.timestamp())
        discovery.update(
            heartbeat_age_seconds=age(lane_times.get("discovery")) if lane_times.get("discovery") else None,
            due_grants=due_discovery_grants(300, now=now).count(),
        )
        event_status = event_routing.summarize(active_grants.values_list("connection__sync_cursor", flat=True), now=now.timestamp())
        window_seconds = options['window_hours'] * 3600 if options['window_hours'] is not None else options['window_minutes'] * 60
        event_deliveries = SlackDmMirrorDelivery.objects.filter(metadata__event_routing_v1__isnull=False)
        outstanding = event_deliveries.exclude(status="completed")
        oldest_parked = outstanding.aggregate(value=Min(Cast("metadata__event_routing_v1__parked_at", FloatField())))["value"]
        event_status["delivery"] = event_routing.summarize_deliveries(
            event_deliveries.filter(status="completed", metadata__event_routing_v1__completed_at__gte=now.timestamp() - window_seconds)
            .order_by("-completed_at", "-id").values_list("metadata__event_routing_v1", "status")[:5001],
            now=now.timestamp(),
            window_seconds=window_seconds,
            completed_only=True,
        )
        event_status["delivery"].update(
            outstanding_by_status=list(outstanding.values("status").annotate(count=Count("id")).order_by("status")),
            oldest_outstanding_age_seconds=max(0, int(now.timestamp() - oldest_parked)) if oldest_parked is not None else 0,
            noop_completed=event_deliveries.filter(status="completed", metadata__event_routing_v1__completed_at__isnull=True).count(),
        )
        try:
            latency = telemetry.latency_snapshot(minutes=options['window_minutes'], hours=options['window_hours'])
            latency['available'] = True
        except Exception:
            latency = {'available': False}
        public_dependency = {}
        for platform in ('buzz', 'slack', 'discord'):
            deliveries = CommunityBridgeDelivery.objects.filter(target_platform=platform)
            waiting = deliveries.filter(status='waiting_parent')
            public_dependency[platform] = {
                'waiting_count': waiting.count(),
                'parked_count': waiting.filter(last_error__startswith='parent_mapping_parked:').count(),
                'dead_unresolvable_by_code': dict(Counter(err.split(':', 2)[1] if ':' in err else 'unknown' for err in
                    deliveries.filter(status='dead', last_error__startswith='parent_unresolvable:').values_list('last_error', flat=True)[:1000])),
                'noop_completed_7d': deliveries.filter(status='completed', completed_at__gte=now - timedelta(days=7), last_error__startswith='dependency_noop:').count(),
                'oldest_waiting_age_seconds': age(waiting.aggregate(value=Min('dependency_first_seen_at'))['value']),
            }
        result = {
            'enabled': enabled, 'checked_at': now.isoformat(), 'stale_lanes': stale,
            'community_source': community_source.status(
                cutoff_ns=cutoff_ns, finalize=options['finalize_community_source']),
            'provider_throughput': throughput,
            'public_dependency': public_dependency,
            'discovery': discovery, 'open_requests': opens, 'open_latency': latency,
            'event_routing': event_status,
            'inbox_pending': inbox.count(),
            'oldest_inbox_age_seconds': age(inbox.aggregate(value=Min('received_at'))['value']),
            'inbox_retrying': inbox.exclude(last_error_code='').count(),
            'due_jobs': list(due.values('kind').annotate(count=Count('id'), oldest_due_at=Min('due_at')).order_by('kind')),
            'expired_job_leases': BridgeSyncJob.objects.filter(lease_expires_at__lte=now).count(),
            'source_coverage': list(BridgeSyncState.objects.values('status').annotate(count=Count('id'), oldest_scan=Min('last_successful_scan_at')).order_by('status')),
            'provider_cooldowns': list(BridgeApiBudget.objects.filter(cooldown_until__gt=now).exclude(method__startswith='priority:').values('method').annotate(count=Count('id'), until=Max('cooldown_until')).order_by('method')),
            'public_delivery': delivery(CommunityBridgeDelivery.objects.filter(target_platform='buzz')),
            'private_delivery': delivery(SlackDmMirrorDelivery.objects.all()),
            'private_undeliverable_by_reason': dict(Counter(str(r or 'unknown') for r in
                SlackDmMirrorDelivery.objects.filter(metadata__undeliverable=True, updated_at__gte=now - timedelta(days=7))
                .values_list('metadata__undeliverable_reason', flat=True)[:5000])),
            'workers': list(BridgeWorkerHeartbeat.objects.values(
                'worker_id', 'lane', 'heartbeat_at', 'progressed_at', 'completed_count', 'failed_count', 'last_error_code',
            )),
        }
        self.stdout.write(json.dumps(result, default=str, sort_keys=True))


def summarize_open_discovery(cursors, *, now):
    """Aggregate queue health without returning keys, source IDs or contents."""
    errors, opens, oldest = Counter(), Counter(), {"pending": 0, "importing": 0}
    leased = 0
    def finite(value):
        return float(value) if type(value) in (int, float) and math.isfinite(value) else 0
    for cursor in cursors:
        if not isinstance(cursor, dict):
            continue
        discovery = cursor.get(DISCOVERY_KEY) or {}
        if isinstance(discovery, dict):
            leased += int(finite(discovery.get("expires")) > now)
            if discovery.get("error"):
                errors[safe_error_code(discovery['error'])] += 1
        requests = cursor.get(OPEN_KEY) or {}
        if not isinstance(requests, dict):
            continue
        for request in requests.values():
            if not isinstance(request, dict) or finite(request.get("until")) <= now:
                continue
            state = request.get("state")
            if state not in {"pending", "importing", "error"}:
                continue
            opens[state] += 1
            stamp = finite(request.get("requested_at"))
            if state in oldest and stamp > 0:
                oldest[state] = max(oldest[state], max(0, int(now - stamp)))
    deferred = errors.pop("BudgetDeferred", 0)
    return ({"leased_grants": leased, "budget_deferred_grants": deferred, "error_codes": dict(errors)},
            {**{name: opens[name] for name in ("pending", "importing", "error")},
             **{f"oldest_{name}_age_seconds": value for name, value in oldest.items()}})
