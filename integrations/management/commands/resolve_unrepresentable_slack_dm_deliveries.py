"""Inspect or explicitly repair unrepresentable private Slack deliveries."""

from collections import Counter, defaultdict
import hashlib
import json
import re

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from integrations.models import SlackDmMirrorDelivery, SlackDmMirrorConversation, SlackDmMirrorGrant
from integrations.services.slack_dm_author_resolution import plan_row, mark_undeliverable_locked


class Command(BaseCommand):
    help = 'Dry-run first: classify unrepresentable Slack deliveries without exposing content.'

    def add_arguments(self, parser):
        selection = parser.add_mutually_exclusive_group(required=True)
        selection.add_argument('--delivery-id', action='append', type=int)
        selection.add_argument('--stuck', action='store_true')
        parser.add_argument('--limit', type=int, default=200)
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--expected-plan')

    def handle(self, *args, **options):
        if not 1 <= options['limit'] <= 500:
            raise CommandError('limit must be between 1 and 500')
        expected = options['expected_plan']
        if expected is not None and not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise CommandError('expected-plan must be a SHA-256 fingerprint')
        if options['apply'] and not expected:
            raise CommandError('--apply requires --expected-plan')
        rows = SlackDmMirrorDelivery.objects.filter(source_platform='slack', status__in=('pending', 'failed'))
        rows = rows.filter(last_error__contains='Slack author is not part of this owner mirror.') if options['stuck'] else rows.filter(pk__in=options['delivery_id'])
        rows = list(rows.select_related('conversation__grant__connection').order_by('id')[:options['limit']])
        now = timezone.now()
        plans = [plan_row(row, now=now) for row in rows]
        fingerprint = hashlib.sha256(json.dumps(sorted([
            [row.pk, plan['action'], plan['verdict'], row.conversation.participant_hash, row.conversation.grant.consent_version]
            for row, plan in zip(rows, plans)
        ]), separators=(',', ':')).encode()).hexdigest()
        if options['apply'] and fingerprint != expected:
            raise CommandError('plan_changed')
        conversations = defaultdict(Counter)
        for plan in plans:
            self.stdout.write(json.dumps(plan, sort_keys=True))
            conversations[plan['conversation_id']][plan['verdict']] += 1
        self.stdout.write(json.dumps(dict(by_verdict=Counter(p['verdict'] for p in plans),
            by_action=Counter(p['action'] for p in plans), by_conversation=conversations,
            participant_hash_stale=sum(p['action'] == 'skip_participants_changed' for p in plans), plan=fingerprint), sort_keys=True))
        if not options['apply']:
            return
        applied = Counter()
        changed = 0
        for original, plan in zip(rows, plans):
            with transaction.atomic():
                get_user_model().objects.select_for_update().get(pk=original.conversation.grant.user_id)
                grant = SlackDmMirrorGrant.objects.select_for_update().select_related('connection').get(pk=original.conversation.grant_id)
                conv = SlackDmMirrorConversation.objects.select_for_update().get(pk=original.conversation_id, grant=grant)
                row = SlackDmMirrorDelivery.objects.select_for_update().get(pk=original.pk, conversation=conv)
                conv.grant = grant
                row.conversation = conv
                current = plan_row(row, now=timezone.now())
                if row.status not in {'pending', 'failed'} or current['action'] != plan['action'] or current['verdict'] != plan['verdict']:
                    changed += 1
                    continue
                if current['action'] == 'mark_undeliverable':
                    mark_undeliverable_locked(row, reason=current['verdict'], source='ops_command')
                    applied[current['action']] += 1
                elif current['action'] == 'request_identity_refresh':
                    metadata = dict(row.metadata or {})
                    metadata.setdefault('author_refresh_requested_at', timezone.now().isoformat())
                    row.metadata = metadata
                    row.save(update_fields=('metadata',))
                    applied[current['action']] += 1
        self.stdout.write(json.dumps({'applied': applied, 'changed': changed}, sort_keys=True))
