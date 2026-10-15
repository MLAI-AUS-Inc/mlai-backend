"""Review public dependency outcomes before an explicitly fingerprinted repair."""

from collections import Counter
from datetime import timedelta
import hashlib
import json
import re

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from integrations.models import CommunityBridgeDelivery
from integrations.services.community_bridge.dependencies import DependencyVerdict, classify_missing_dependency, apply_dependency_verdict
from integrations.services.community_bridge import store
from integrations.services.message_sync.delivery import guard_delivery
from integrations.services.message_sync.scheduler import LeaseLost
from integrations.services.message_sync.reaction_recovery import LEGACY_EMOJI


def dependency_id(row):
    """Retain colon-containing reaction identifiers from both parked formats."""
    error = str(row.last_error or '')
    match = re.fullmatch(r'parent_mapping_pending:(.+)', error) or re.fullmatch(r'parent_mapping_parked:([a-z0-9_]+):(.+)', error)
    return match.groups()[-1] if match else (row.source_parent_message_id if row.delivery_type in {'create', 'reaction_add'} else row.source_message_id)


class Command(BaseCommand):
    help = 'Content-free dry run of public bridge dependencies; repairs require the exact plan.'

    def add_arguments(self, parser):
        parser.add_argument('--channel-id')
        parser.add_argument('--limit', type=int, default=500)
        for key in ('include-dead-reaction-adds', 'flatten-replies', 'confirm-flatten', 'requeue-dead-reaction-adds', 'confirm-adapter-reaction-fix', 'apply'):
            parser.add_argument('--' + key, action='store_true')
        parser.add_argument('--expected-plan')

    def _review(self, row, options, now):
        parent = dependency_id(row)
        first_seen = row.dependency_first_seen_at or row.created_at
        verdict = classify_missing_dependency(row, parent, first_seen=first_seen, now=now)
        # The worker rechecks this under its row lock before classification.
        # Operator review needs the same guard for historical missed wakes.
        link = store.resolve_mapped_message(source_platform=row.source_platform,
            source_channel_id=row.source_channel_id, source_message_id=parent,
            destination_platform=row.target_platform) if row.status == 'waiting_parent' else None
        if link and str(link.get('destination_message_id') or '').strip():
            verdict = DependencyVerdict('wait', 'dependency_already_mapped', verdict.kind)
        action = 'keep' if verdict.action == 'wait' else verdict.action
        if row.status == 'dead':
            later_remove = CommunityBridgeDelivery.objects.filter(channel_id=row.channel_id,
                source_channel_id=row.source_channel_id, source_message_id=row.source_message_id,
                delivery_type='reaction_remove', id__gt=row.pk).exists()
            action = 'keep_dead' if later_remove else 'requeue_reaction_add' if options['requeue_dead_reaction_adds'] else 'requeue_candidate'
        elif (options['flatten_replies'] and row.delivery_type == 'create' and verdict.action in {'park', 'dead_letter'}
              and row.source_platform == 'slack' and row.target_platform == 'buzz'
              and not row.canonical_envelope and not (row.payload or {}).get('_buzz_envelope_v1')):
            action = 'flatten'
        return dict(delivery_id=row.pk, slack_channel_id=row.channel.slack_channel_id,
            delivery_type=row.delivery_type, source_platform=row.source_platform, target_platform=row.target_platform,
            dependency_kind=verdict.kind,
            dependency_age_days=max(0, (now.timestamp() - float(parent)) / 86400) if verdict.kind == 'slack_ts' else None,
            waiting_days=max(0, (now - first_seen).total_seconds() / 86400), dependency_attempts=row.dependency_attempts,
            code=verdict.code, evidence=verdict.evidence, action=action), verdict, parent, first_seen

    def handle(self, *args, **options):
        if not 1 <= options['limit'] <= 2000:
            raise CommandError('limit must be between 1 and 2000')
        if options['channel_id'] and not re.fullmatch(r'[A-Z0-9]+', options['channel_id']):
            raise CommandError('invalid channel-id')
        expected = options['expected_plan']
        if expected is not None and not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise CommandError('invalid expected-plan')
        if options['flatten_replies'] and not options['confirm_flatten']:
            raise CommandError('--flatten-replies requires --confirm-flatten')
        if options['requeue_dead_reaction_adds'] and not (options['include_dead_reaction_adds'] and options['confirm_adapter_reaction_fix']):
            raise CommandError('reaction requeue requires inclusion and adapter confirmation')
        if options['apply'] and not expected:
            raise CommandError('--apply requires --expected-plan')
        now = timezone.now()
        query = CommunityBridgeDelivery.objects.select_related('channel').order_by('id')
        if options['channel_id']:
            query = query.filter(channel__slack_channel_id=options['channel_id'])
        rows = list(query.filter(status='waiting_parent')[:options['limit']])
        if options['include_dead_reaction_adds'] and len(rows) < options['limit']:
            candidates = query.filter(status='dead', delivery_type='reaction_add', source_platform='slack', target_platform='buzz',
                created_at__gte=now - timedelta(days=30),
                last_error__startswith='BuzzBridgePermanentError: MLAI Chat adapter rejected delivery with HTTP 400')
            for row in candidates.iterator(chunk_size=200):
                if ((row.payload or {}).get('text') not in LEGACY_EMOJI
                        or '[invalid_delivery/reaction_add_is_not_in_the_approved_bridge_set]' in row.last_error):
                    rows.append(row)
                    if len(rows) >= options['limit']:
                        break
            rows = sorted(rows, key=lambda row: row.pk)[:options['limit']]
        reviewed = [(row, *self._review(row, options, now)) for row in rows]
        plans = [entry[1] for entry in reviewed]
        fingerprint = hashlib.sha256(json.dumps(sorted([[p['delivery_id'], p['action'], p['code']] for p in plans]), separators=(',', ':')).encode()).hexdigest()
        if options['apply'] and expected != fingerprint:
            raise CommandError('plan_changed')
        for plan in plans:
            self.stdout.write(json.dumps(plan, sort_keys=True))
        self.stdout.write(json.dumps(dict(by_code=Counter(p['code'] for p in plans), by_action=Counter(p['action'] for p in plans),
            by_target_platform=Counter(p['target_platform'] for p in plans), plan=fingerprint), sort_keys=True))
        if not options['apply']:
            return
        counts = Counter()
        for original, plan, verdict, parent, first_seen in sorted(reviewed, key=lambda entry: (entry[1]['action'] == 'requeue_reaction_add', entry[0].pk)):
            if plan['action'] in {'keep', 'keep_dead', 'requeue_candidate'}:
                continue
            with transaction.atomic():
                row = CommunityBridgeDelivery.objects.select_for_update().select_related('channel').get(pk=original.pk)
                try:
                    guard_delivery(row)
                except LeaseLost:
                    counts['skipped_claimed'] += 1
                    continue
                current, current_verdict, current_parent, current_first_seen = self._review(row, options, now)
                if row.status != original.status or current_parent != parent or current['code'] != plan['code'] or current['action'] != plan['action']:
                    counts['skipped_changed'] += 1
                    continue
                action = plan['action']
                if action in {'noop', 'dead_letter', 'park'}:
                    apply_dependency_verdict(row, current_verdict, dependency_id=parent, first_seen=current_first_seen, now=now, claimed=False)
                elif action == 'flatten':
                    payload = dict(row.payload or {})
                    metadata = dict(payload.get('metadata') or {})
                    metadata.update(original_thread_ts=parent, thread_parent_unavailable=True)
                    payload.update(metadata=metadata, source_parent_message_id='')
                    row.payload = payload
                    row.source_parent_message_id = ''
                    row.status = 'pending'
                    row.available_at = now
                    row.completed_at = None
                    row.last_error = f'parent_flattened:{verdict.code}:{parent}'
                    row.save(update_fields=('payload', 'source_parent_message_id', 'status', 'available_at', 'completed_at', 'last_error', 'updated_at'))
                elif action == 'requeue_reaction_add':
                    row.status = 'pending'
                    row.attempts = 0
                    row.available_at = now
                    row.locked_at = row.lease_token = row.lease_expires_at = row.completed_at = None
                    row.last_error = ''
                    row.save(update_fields=('status', 'attempts', 'available_at', 'locked_at', 'lease_token', 'lease_expires_at', 'completed_at', 'last_error', 'updated_at'))
                counts[action] += 1
        self.stdout.write(json.dumps(dict(applied=counts), sort_keys=True))
