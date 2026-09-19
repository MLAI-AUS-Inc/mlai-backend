"""Content-free diagnostics and worker liveness for the durable sync lanes."""
import json
import socket
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Max, Min
from django.utils import timezone
from integrations.models import (
    BridgeApiBudget, BridgeSyncInbox, BridgeSyncJob, BridgeSyncState,
    BridgeWorkerHeartbeat, CommunityBridgeDelivery, SlackDmMirrorDelivery,
)
from integrations.services.message_sync import telemetry
from integrations.services.message_sync.scheduler import eligible_states
from integrations.services.message_sync.slack_client import provider_interval


class Command(BaseCommand):
    help = "Show sync backlog, provider cooldowns, coverage and worker health without message content."

    def add_arguments(self, parser):
        parser.add_argument('--check', action='store_true', help='Fail if an enabled worker lane is stale or absent.')
        parser.add_argument('--local-worker', action='store_true', help='Check only this container, not a previous deployment.')
        parser.add_argument('--max-heartbeat-age', type=int, default=180)
        parser.add_argument('--window-minutes', type=int, default=5,
                            help='Completed minute buckets for provider throughput (1–15).')

    def handle(self, *args, **options):
        enabled = bool(getattr(settings, 'MESSAGE_SYNC_ENABLED', False))
        if options['max_heartbeat_age'] < 1:
            raise CommandError('--max-heartbeat-age must be positive')
        if not 1 <= options['window_minutes'] <= 15:
            raise CommandError('--window-minutes must be between 1 and 15')
        if options['check'] and not enabled:
            self.stdout.write(json.dumps({'enabled': False}))
            return
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
            throughput = telemetry.snapshot(scopes, minutes=options['window_minutes'])
            rows = []
            for scope, counters in throughput.pop('scopes').items():
                if counters is None:
                    continue
                budget = scopes[scope]
                allowance = 60 / provider_interval(budget.method)
                rows.append({'scope': scope, 'method': budget.method, **counters,
                             'requests_per_minute': round(counters['admitted'] / options['window_minutes'], 2),
                             'configured_requests_per_minute': allowance,
                             'configured_budget_used_percent': round(100 * counters['admitted'] / (options['window_minutes'] * allowance), 1),
                             'mean_request_ms': round(counters['request_ms'] / counters['finished']) if counters['finished'] else None})
            throughput.update(available=True, measured_scopes=rows)
        except Exception:
            throughput = {'available': False}
        result = {
            'enabled': enabled, 'checked_at': now.isoformat(), 'stale_lanes': stale,
            'provider_throughput': throughput,
            'inbox_pending': inbox.count(),
            'oldest_inbox_age_seconds': age(inbox.aggregate(value=Min('received_at'))['value']),
            'inbox_retrying': inbox.exclude(last_error_code='').count(),
            'due_jobs': list(due.values('kind').annotate(count=Count('id'), oldest_due_at=Min('due_at')).order_by('kind')),
            'expired_job_leases': BridgeSyncJob.objects.filter(lease_expires_at__lte=now).count(),
            'source_coverage': list(BridgeSyncState.objects.values('status').annotate(count=Count('id'), oldest_scan=Min('last_successful_scan_at')).order_by('status')),
            'provider_cooldowns': list(BridgeApiBudget.objects.filter(cooldown_until__gt=now).values('method').annotate(count=Count('id'), until=Max('cooldown_until')).order_by('method')),
            'public_delivery': delivery(CommunityBridgeDelivery.objects.filter(target_platform='buzz')),
            'private_delivery': delivery(SlackDmMirrorDelivery.objects.all()),
            'workers': list(BridgeWorkerHeartbeat.objects.values(
                'worker_id', 'lane', 'heartbeat_at', 'progressed_at', 'completed_count', 'failed_count', 'last_error_code',
            )),
        }
        self.stdout.write(json.dumps(result, default=str, sort_keys=True))
