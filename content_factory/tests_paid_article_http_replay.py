"""Generic paid article HTTP retries, with real admission, wallet and outbox.

The authenticated route is not the first-article command endpoint. The only
external effect is an isolated worker transport; the fixture's normal billing,
balance and activation stubs are replaced with their actual implementations.
"""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from uuid import uuid4
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import requests
from django.test import TransactionTestCase, override_settings
from django.db import close_old_connections, connection, connections
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.test import APIClient

from integrations.services import article_generation as generation
from roo.models import Ledger
from roo.services import PointsService
from workflow_runs.models import ContentFactoryRun
from . import billing, content_first_article as cards, paid_article_commands as commands, vibe_marketing_views as views
from .dispatch_models import ContentFactoryDispatchOutbox
from .run_event_authority import receive_authoritative_run_events
from .run_ownership import OWNER_RESULT_KEY
from .tests_content_first_article import CardFixture
from .tests_run_event_authority import FLAGS, batch


actual_admission = views._validate_article_start_admission
actual_balance = views._roo_points_balance_for_user


def authenticated_client(user):
    from community_chat.account_sessions import issue_account_session
    from community_chat.models import CommunityMemberProfile
    CommunityMemberProfile.objects.update_or_create(user=user, defaults={'status': 'approved'})
    credentials = issue_account_session(user, SimpleNamespace(client_id='mlai-chat-ios',
        installation_id=uuid4(), origin='mlaichat://callback', platform='ios',
        device_name='Synthetic paid HTTP replay',
        public_key=hashlib.sha256(f'paid-http-replay:{user.pk}'.encode()).hexdigest()))
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {credentials.access_token}')
    return client


@override_settings(**FLAGS, ARTICLES_RUN_CONTROL_MODEL_USD='5', ARTICLES_RUN_CONTROL_MODEL_TOKENS=100000)
class PaidArticleHttpReplayTests(CardFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(ARTICLES_FREE_FIRST_ARTICLE_ENABLED=False,
            CONTENT_FACTORY_URL='https://paid-article.fixture.invalid',
            CONTENT_FACTORY_API_KEY='synthetic-paid-article-key'))
        self.enterContext(patch.object(views, 'charge_content_factory_request_for_user',
            side_effect=generation.charge_content_factory_request_for_user))
        self.enterContext(patch.object(views, 'get_content_factory_article_cost_points',
            side_effect=billing.get_content_factory_article_cost_points))
        self.enterContext(patch.object(views, '_roo_points_balance_for_user', side_effect=actual_balance))
        self.enterContext(patch.object(views, '_validate_article_start_admission', side_effect=actual_admission))
        PointsService.credit_purchased_topup(user=self.owner, delta=100,
            description='Synthetic generic paid article funding', idempotency_key='generic-paid-funding')
        self.body = self.approved_body(self.built)
        self.body['costConfirmed'] = True
        self.url = self.route(self.company)
        self.client = authenticated_client(self.owner)
        self.sent = []

        def worker(url, *, json, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.sent.append(deepcopy(json))
            self.assertIn('backend_run_admission_v1', json)
            response = requests.Response()
            response.status_code = 202
            response._content = b'{"run_id":"generic-paid-worker","status":"queued"}'
            response._content_consumed = True
            return response

        self.enterContext(patch('integrations.http_client.post', side_effect=worker))

    def approved_body(self, built):
        context = SimpleNamespace(organization=built.organization, company=built.company,
            profile=built.company.profile)
        press = cards.parse_press({'customTopic': 'Booking reminders for busy clinics', 'expectedCostPoints': 12})
        prepared = cards._prepare(context, press, built.config)
        brief = cards._approve(context, self.owner, built.config, prepared)
        return cards._start_data(prepared['topic'], brief,
            client_request_id='generic-paid-caller-key', expected=12)

    def route(self, company):
        return f'/api/v1/my-startup/vibe-marketing/article/?companyId={company.pk}'

    def post(self, *, body=None, client=None, url=None):
        data = self.body if body is None else body
        return (client or self.client).post(url or self.url, data, format='json',
            HTTP_IDEMPOTENCY_KEY='generic-paid-caller-key')

    def start(self):
        response = self.post()
        self.assertEqual(response.status_code, 202, response.data)
        run = ContentFactoryRun.objects.get(run_id=response.data['runId'])
        self.assertIn(OWNER_RESULT_KEY, run.result)
        self.assertEqual(run.run_request['client_request_id'], self.body['client_request_id'])
        self.assertEqual(Ledger.objects.filter(kind='SPEND').count(), 1)
        self.assertEqual(ContentFactoryDispatchOutbox.objects.count(), 1)
        self.assertEqual(len(self.sent), 1)
        return run

    def financial_dispatch_state(self):
        return {'ledger': list(Ledger.objects.order_by('pk').values()),
            'outbox': list(ContentFactoryDispatchOutbox.objects.order_by('pk').values()),
            'runs': list(ContentFactoryRun.objects.order_by('pk').values()),
            'sends': len(self.sent)}

    def test_successful_response_lost_to_caller_replays_original_paid_run_without_second_effect(self):
        run = self.start()  # Its HTTP response is deliberately discarded by the caller.
        before = self.financial_dispatch_state()
        replay = self.post()
        self.assertEqual(replay.status_code, 202, replay.data)
        self.assertEqual(replay.data['runId'], run.run_id)
        self.assertEqual(self.financial_dispatch_state(), before)
        target = os.environ.get('FOUNDER_PAID_WIRE_EXPORT')
        if target:
            Path(target).write_text(json.dumps({'schema_version': 1,
                'provenance': 'Real account bearer authentication and exact generic My startup facade; worker transport isolated.',
                'company_id': str(self.company.pk), 'user_id': self.owner.pk,
                'request': {'method': 'POST', 'path': self.url,
                    'headers': {'Idempotency-Key': 'generic-paid-caller-key'}, 'body': self.body},
                'response': {'status': replay.status_code, 'body': replay.json()},
                'replay': {'run_id': run.run_id, 'spends': 1, 'outboxes': 1, 'worker_posts': 1}}, indent=2) + '\n')

    def test_actual_full_client_shape_with_saved_author_replays_unchanged(self):
        from .authors import normalize_authors
        author_id, key = str(uuid4()), str(uuid4())
        self.config.authors = normalize_authors([{'id': author_id, 'name': 'Reviewed clinic founder',
            'role': 'Founder', 'bio': 'The owner-approved byline for this synthetic startup.'}])
        self.config.default_author_id = author_id
        self.config.save(update_fields=['authors', 'default_author_id'])
        fields = ('audience_id', 'audience_version', 'conversion_intent', 'offer_id', 'offer_version',
            'no_offer_reason', 'country', 'reader_task', 'distinct_contribution', 'acceptance_criteria')
        brief = {name: self.body['editorial_brief'][name] for name in fields}
        body = {'companyId': str(self.company.pk), 'client_request_id': key, 'authorId': author_id,
            'editorialBrief': brief, 'expectedCostPoints': 12, 'costConfirmed': True,
            'topic': self.body['topic'], 'targetKeyword': self.body['target_keyword'],
            'deliveryMode': 'content_only', 'deliveryModeExplicit': True, 'deliveryModeConfirmed': True}
        first = self.client.post(self.url, body, format='json', HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(first.status_code, 202, first.data)
        run = ContentFactoryRun.objects.get(run_id=first.data['runId'])
        self.assertEqual(run.run_request['author_id'], author_id)
        self.assertEqual(run.run_request['client_request_id'], key)
        self.assertEqual(run.run_request['default_author_profile']['personId'], author_id)
        before = self.financial_dispatch_state()
        replay = self.client.post(self.url, deepcopy(body), format='json', HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual((replay.status_code, replay.json()), (202, first.json()))
        self.assertEqual(self.financial_dispatch_state(), before)
        self.assertEqual((Ledger.objects.filter(kind='SPEND').count(), ContentFactoryDispatchOutbox.objects.count(),
            len(self.sent)), (1, 1, 1))
        target = os.environ.get('FOUNDER_PAID_CLIENT_WIRE_EXPORT')
        if target:
            Path(target).write_text(json.dumps({'schema_version': 1,
                'provenance': 'Actual account bearer + exact My startup facade; saved owner-approved author and catalogue brief; isolated worker HTTP.',
                'company_id': str(self.company.pk), 'author_id': author_id, 'user_id': self.owner.pk,
                'request': {'method': 'POST', 'path': self.url, 'headers': {'Idempotency-Key': key}, 'body': body},
                'response': {'status': first.status_code, 'body': first.json()},
                'replay_response': {'status': replay.status_code, 'body': replay.json()},
                'effects': {'run_id': run.run_id, 'spends': 1, 'outboxes': 1, 'worker_posts': 1}}, indent=2) + '\n')

    def test_completed_original_replays_even_when_its_topic_is_now_covered(self):
        from .models import WrittenArticle
        run = self.start()
        response = receive_authoritative_run_events(batch(run, [{'status': 'completed'}]))
        self.assertEqual(response.status_code, 200, response.data)
        WrittenArticle.objects.create(organization=self.org, title=self.body['topic'],
            primary_keyword=self.body['target_keyword'], slug='original-completed-topic', source_run_id=run.run_id)
        before = self.financial_dispatch_state()
        replay = self.post()
        self.assertEqual(replay.status_code, 202, replay.data)
        self.assertEqual(replay.data['runId'], run.run_id)
        self.assertEqual(self.financial_dispatch_state(), before)

    def test_changed_original_body_is_a_terminal_conflict_without_new_work(self):
        self.start()
        before = self.financial_dispatch_state()
        replay = self.post(body={**self.body, 'context': 'A different approved article request'})
        self.assertEqual(replay.status_code, 409, replay.data)
        self.assertEqual(self.financial_dispatch_state(), before)

    def test_actual_original_refund_prevents_paid_retry_without_new_work(self):
        self.start()
        spend = Ledger.objects.get(kind='SPEND')
        PointsService.refund(user=self.owner, delta=12, source=spend.source,
            description='Synthetic original paid article refund', idempotency_key='generic-paid-original-refund',
            created_by_slack_id=f'mlai_user:{self.owner.pk}', original_spend_key=spend.idempotency_key,
            reference_type=spend.reference_type, reference_id=spend.reference_id)
        before = self.financial_dispatch_state()
        replay = self.post()
        self.assertEqual(replay.status_code, 409, replay.data)
        self.assertEqual(self.financial_dispatch_state(), before)

    def test_revoked_current_principal_cannot_replay_original_paid_command(self):
        self.start()
        stale_authenticated_client = APIClient()
        stale_authenticated_client.force_authenticate(self.owner)
        type(self.owner).objects.filter(pk=self.owner.pk).update(is_active=False)
        before = self.financial_dispatch_state()
        # This deliberately models deactivation after authentication retained
        # an active user object; the command must still re-read the actual row.
        replay = self.post(client=stale_authenticated_client)
        self.assertEqual(replay.status_code, 403, replay.data)
        self.assertEqual(self.financial_dispatch_state(), before)

    def test_real_account_bearer_authentication_rejects_a_deactivated_original_requester(self):
        self.start()
        type(self.owner).objects.filter(pk=self.owner.pk).update(is_active=False)
        before = self.financial_dispatch_state()
        response = self.post()
        self.assertEqual(response.status_code, 401, response.data)
        self.assertEqual(self.financial_dispatch_state(), before)

    def test_lost_finish_ack_replays_durable_outbox_without_another_provider_call(self):
        with patch.object(commands, 'finish', side_effect=ConnectionError('Synthetic lost HTTP acknowledgement')):
            with self.assertRaises(ConnectionError):
                self.post()
        run = ContentFactoryRun.objects.get()
        before = self.financial_dispatch_state()
        replay = self.post()
        self.assertEqual((replay.status_code, replay.data['runId']), (202, run.run_id))
        self.assertEqual(self.financial_dispatch_state(), before)

    def test_crash_after_real_debit_and_outbox_before_transport_replays_owned_pending_run(self):
        with patch.object(views, '_queue_content_factory_run', side_effect=ConnectionError('Synthetic crash after commit')):
            with self.assertRaises(ConnectionError):
                self.post()
        run = ContentFactoryRun.objects.get()
        self.assertEqual((Ledger.objects.filter(kind='SPEND').count(), ContentFactoryDispatchOutbox.objects.count(),
            len(self.sent)), (1, 1, 0))
        before = self.financial_dispatch_state()
        replay = self.post()
        self.assertEqual((replay.status_code, replay.data['runId']), (202, run.run_id))
        self.assertEqual(self.financial_dispatch_state(), before)

    def test_concurrent_same_key_has_one_debit_and_worker_without_cache_authority(self):
        entered, release = Event(), Event()
        def admission(*args, **kwargs):
            actual_admission(*args, **kwargs)
            entered.set()
            self.assertTrue(release.wait(10))
        def first():
            close_old_connections()
            try:
                client = authenticated_client(self.owner)
                return self.post(client=client)
            finally:
                connections.close_all()
        with patch.object(views, '_validate_article_start_admission', side_effect=admission), \
                patch('django.core.cache.cache.add', side_effect=ConnectionError('Synthetic unavailable cache')), \
                ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(first)
            self.assertTrue(entered.wait(10))
            try:
                replay = self.post()
                self.assertEqual((replay.status_code, replay.data['reason']), (409, 'in_progress'))
            finally:
                release.set()
            response = pending.result(timeout=20)
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual((Ledger.objects.filter(kind='SPEND').count(), ContentFactoryDispatchOutbox.objects.count(),
            ContentFactoryRun.objects.count(), len(self.sent)), (1, 1, 1, 1))
        self.assertEqual(self.post().data['runId'], response.data['runId'])

    def test_deactivation_after_activation_is_rechecked_before_actual_debit(self):
        def revoke(*args, **kwargs):
            actual_admission(*args, **kwargs)
            type(self.owner).objects.filter(pk=self.owner.pk).update(is_active=False)
        with patch.object(views, '_validate_article_start_admission', side_effect=revoke):
            response = self.post()
        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(Ledger.objects.filter(kind='SPEND').exists())
        self.assertFalse(ContentFactoryDispatchOutbox.objects.exists())
        self.assertFalse(ContentFactoryRun.objects.exists())
        self.assertEqual(self.sent, [])

    def test_historical_outbox_without_original_http_body_proof_is_not_adopted(self):
        self.start()
        self.config.refresh_from_db()
        self.config.pillar_strategy.pop(commands.KEY)
        self.config.save(update_fields=['pillar_strategy'])
        row = ContentFactoryDispatchOutbox.objects.get()
        row.payload.pop(commands.OUTBOX_KEY)
        row.save(update_fields=['payload'])
        before = self.financial_dispatch_state()
        response = self.post()
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(self.financial_dispatch_state(), before)
        self.config.refresh_from_db()
        self.assertNotIn(commands.KEY, self.config.pillar_strategy)

    def test_malformed_private_receipt_is_held_without_repair_or_dispatch(self):
        self.start()
        self.config.refresh_from_db()
        strategy = deepcopy(self.config.pillar_strategy)
        digest = next(iter(strategy[commands.KEY]))
        for field, value in [('user_id', float(self.owner.pk)), ('lease_until', None), ('lease_token', True)]:
            broken = deepcopy(strategy)
            broken[commands.KEY][digest][field] = value
            self.config.pillar_strategy = broken
            self.config.save(update_fields=['pillar_strategy'])
            before = self.financial_dispatch_state()
            response = self.post()
            self.assertEqual(response.status_code, 409, response.data)
            self.assertEqual(self.financial_dispatch_state(), before)
            self.config.refresh_from_db()
            self.assertEqual(self.config.pillar_strategy, broken)

    def test_unknown_outbox_state_or_incoherent_run_binding_is_held_verbatim(self):
        self.start()
        original = ContentFactoryDispatchOutbox.objects.get()
        for state, run_id in [('unknown', original.run_id), ('refund_pending', original.run_id),
                ('failed', original.run_id), ('delivered', ''), ('pending', 'another-worker')]:
            ContentFactoryDispatchOutbox.objects.filter(pk=original.pk).update(state=state, run_id=run_id)
            before = self.financial_dispatch_state()
            response = self.post()
            self.assertEqual(response.status_code, 409, response.data)
            self.assertEqual(response.data['reason'], 'operation_conflict')
            self.assertEqual(self.financial_dispatch_state(), before)

    def test_no_key_or_legacy_cohort_does_not_create_a_paid_http_receipt(self):
        context = SimpleNamespace(organization=self.org, company=self.company, profile=self.company.profile)
        for body, overrides in [({key: value for key, value in self.body.items() if key != 'client_request_id'}, {}),
                (self.body, {'MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED': False})]:
            with override_settings(**overrides):
                sentinel = Response({'legacy': True}, status=202)
                response = commands.execute(context=context, user=self.owner, data=body,
                    header='generic-paid-caller-key', start=lambda: sentinel)
            self.assertIs(response, sentinel)
        self.config.refresh_from_db()
        self.assertNotIn(commands.KEY, self.config.pillar_strategy)

    def grant_member(self, *, payer=None):
        from core.models import User
        from founder_tools.services import get_or_create_founder_profile
        member = User.objects.create_user(email='paid-http-member@example.test', email_verified_at=timezone.now())
        get_or_create_founder_profile(member)
        self.org.billing_user = payer or self.owner
        self.org.save(update_fields=['billing_user'])
        roster = {'version': 1, 'owner_user_id': self.owner.pk, 'frozen': False,
            'members': [{'memberId': 'paid-member', 'user_id': member.pk, 'state': 'active',
                'rights': {'billing': True, 'publish': False}}], 'requests': [], 'invite': None, 'history': []}
        self.store(startup_team=roster)
        self.client = authenticated_client(member)
        return member, roster

    @override_settings(MY_STARTUP_TEAM_ENABLED=True)
    def test_actual_member_spend_grant_uses_original_payer_and_revocation_blocks_replay(self):
        member, roster = self.grant_member()
        response = self.post()
        self.assertEqual(response.status_code, 202, response.data)
        run = ContentFactoryRun.objects.get()
        self.assertEqual(run.run_request['roo_points_requested_by_user_id'], member.pk)
        self.assertEqual(run.run_request['roo_points_billing_user_id'], self.owner.pk)
        self.assertEqual(Ledger.objects.get(kind='SPEND').user_id, self.owner.pk)
        self.assertEqual((ContentFactoryDispatchOutbox.objects.count(), len(self.sent)), (1, 1))
        before = self.financial_dispatch_state()
        self.assertEqual(self.post().data['runId'], run.run_id)
        self.assertEqual(self.financial_dispatch_state(), before)
        roster['members'][0]['rights']['billing'] = False
        self.store(startup_team=roster)
        response = self.post()
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(self.financial_dispatch_state(), before)

    @override_settings(MY_STARTUP_TEAM_ENABLED=True)
    def test_member_without_spend_grant_or_with_free_quote_cannot_start_an_obligation(self):
        _, roster = self.grant_member()
        roster['members'][0]['rights']['billing'] = False
        self.store(startup_team=roster)
        self.assertEqual(self.post().status_code, 403)
        roster['members'][0]['rights']['billing'] = True
        self.store(startup_team=roster)
        missing = {key: value for key, value in self.body.items() if key != 'costConfirmed'}
        for body in [missing, {**self.body, 'costConfirmed': False},
                {**self.body, 'cost_confirmed': False}, {**self.body, 'expectedCostPoints': True},
                {**self.body, 'expected_cost_points': 6}]:
            response = self.post(body=body)
            self.assertEqual(response.status_code, 403, response.data)
        with override_settings(ARTICLES_FREE_FIRST_ARTICLE_ENABLED=True):
            response = self.post(body={**self.body, 'expectedCostPoints': 0})
        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(Ledger.objects.filter(kind__in=['SPEND', 'ADJUST']).exists())
        self.assertFalse(ContentFactoryDispatchOutbox.objects.exists())
        self.assertFalse(ContentFactoryRun.objects.exists())
        self.assertEqual(self.sent, [])

    def test_owner_original_quote_consent_without_new_member_flag_stays_compatible(self):
        body = {key: value for key, value in self.body.items() if key != 'costConfirmed'}
        response = self.post(body=body)
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual((Ledger.objects.filter(kind='SPEND').count(), ContentFactoryDispatchOutbox.objects.count(),
            ContentFactoryRun.objects.count(), len(self.sent)), (1, 1, 1, 1))

    @override_settings(MY_STARTUP_TEAM_ENABLED=True)
    def test_member_scope_never_authorizes_non_http_repository_or_free_entitlement_work(self):
        from core.actor_ids import preferred_actor_id_for_user
        from founder_tools.services import get_or_create_founder_profile
        from .activation import founder_context_for_domain
        member, _ = self.grant_member()
        actor = preferred_actor_id_for_user(member)
        payload = {**self.body, 'domain': self.org.domain, 'slack_user_id': actor, 'github_repo': ''}
        # A service/MCP call, even with caller-supplied private-looking fields,
        # has no trusted HTTP claim and retains the original owner-only gate.
        with self.assertRaises(generation.ArticleGenerationError):
            generation.require_article_activation(domain=self.org.domain, actor_id=actor, user=member,
                article_request={**payload, commands.KEY: {'user_id': member.pk}})
        context = SimpleNamespace(organization=self.org, company=self.company,
            profile=get_or_create_founder_profile(member))
        claim, replay = commands._claim(context, member, self.body, 'generic-paid-caller-key', allow_new=True)
        self.assertIsNone(replay)
        token = commands._active_claim.set((context, member, claim))
        try:
            self.assertIs(commands.portable_member_context(user=member, actor_id=actor,
                domain=self.org.domain, article_request=payload), context)
            for changes in [{'github_repo': 'foreign/repository'}, {'website_connection_id': '42'},
                    {'operation_id': 'setup-operation'}, {'github_token': 'synthetic-token'},
                    {'delivery_mode': 'review_draft'}, {'delivery_mode_confirmed': False},
                    {'client_request_id': 'another-command'}, {'expectedCostPoints': 0}]:
                with self.assertRaises(commands.PaidArticleCommandRefusal):
                    commands.portable_member_context(user=member, actor_id=actor,
                        domain=self.org.domain, article_request={**payload, **changes})
            with self.assertRaises(commands.PaidArticleCommandRefusal):
                commands.portable_member_context(user=member, actor_id=actor,
                    domain='foreign.example.test', article_request=payload)
            with self.assertRaises(generation.ArticleGenerationError):
                generation.require_article_activation(domain=self.org.domain, actor_id=actor, user=member,
                    expected_repo='owner/website', article_request=payload)
            # The global context used by setup/publish/credential callers stays
            # owner-only even while this narrow portable member claim is active.
            with self.assertRaises(PermissionError):
                founder_context_for_domain(member, self.org.domain)
        finally:
            commands._active_claim.reset(token)
            commands.finish(context, claim)
        self.assertFalse(Ledger.objects.filter(kind__in=['SPEND', 'ADJUST']).exists())
        self.assertFalse(ContentFactoryDispatchOutbox.objects.exists())
        self.assertEqual(self.sent, [])

    @override_settings(MY_STARTUP_TEAM_ENABLED=True)
    def test_member_payer_and_grant_are_rechecked_after_actual_activation_before_debit(self):
        from core.models import User
        from founder_tools.services import get_or_create_founder_profile
        from founder_tools.models import VibeRaisingCompany
        payer = User.objects.create_user(email='opted-in-payer@example.test')
        profile = get_or_create_founder_profile(payer)
        company = VibeRaisingCompany.objects.create(profile=profile, organization=self.org,
            name='Explicit company payer', domain=self.org.domain)
        PointsService.credit_purchased_topup(user=payer, delta=100,
            description='Synthetic opted-in company payer', idempotency_key='generic-member-payer')
        member, roster = self.grant_member(payer=payer)
        for change in ['payer_inactive', 'payer_transferred', 'payer_opted_out', 'member_spend_revoked', 'owner_inactive']:
            type(self.owner).objects.filter(pk__in=[self.owner.pk, payer.pk]).update(is_active=True)
            VibeRaisingCompany.objects.filter(pk=company.pk).update(organization=self.org)
            type(self.org).objects.filter(pk=self.org.pk).update(billing_user=payer)
            self.store(startup_team=roster)
            self.client = authenticated_client(member)
            def revoke(*args, **kwargs):
                actual_admission(*args, **kwargs)
                if change == 'payer_inactive':
                    User.objects.filter(pk=payer.pk).update(is_active=False)
                elif change == 'payer_transferred':
                    VibeRaisingCompany.objects.filter(pk=company.pk).update(organization=None)
                elif change == 'payer_opted_out':
                    type(self.org).objects.filter(pk=self.org.pk).update(billing_user=None)
                elif change == 'owner_inactive':
                    User.objects.filter(pk=self.owner.pk).update(is_active=False)
                else:
                    revoked = deepcopy(roster)
                    revoked['members'][0]['rights']['billing'] = False
                    self.store(startup_team=revoked)
            with patch.object(views, '_validate_article_start_admission', side_effect=revoke):
                response = self.post()
            self.assertEqual(response.status_code, 403, (change, response.data))
            self.assertFalse(Ledger.objects.filter(kind='SPEND').exists())
            self.assertFalse(ContentFactoryDispatchOutbox.objects.exists())
            self.assertFalse(ContentFactoryRun.objects.exists())
            self.assertEqual(self.sent, [])
    def test_retained_command_replays_after_admission_flag_off_without_legacy_dispatch(self):
        run = self.start()
        context = SimpleNamespace(organization=self.org, company=self.company, profile=self.company.profile)
        before = self.financial_dispatch_state()
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
            response = commands.execute(context=context, user=self.owner, data=self.body,
                header='generic-paid-caller-key', start=lambda: self.fail('A retained command cannot dispatch again'))
        self.assertEqual((response.status_code, response.data['runId']), (202, run.run_id))
        self.assertEqual(self.financial_dispatch_state(), before)

    def test_enrolled_namespace_refuses_unseen_http_keys_after_admission_flag_off(self):
        run = self.start()
        self.client.raise_request_exception = False
        key = str(uuid4())
        body = {**deepcopy(self.body), 'client_request_id': key,
            'topic': 'A different clinic workflow', 'target_keyword': 'A different clinic workflow'}
        before = self.financial_dispatch_state()
        self.config.refresh_from_db()
        strategy_before = deepcopy(self.config.pillar_strategy)
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
            response = self.client.post(self.url, body, format='json', HTTP_IDEMPOTENCY_KEY=key)
            retained = self.post()
        self.assertEqual(response.status_code, 409, getattr(response, 'data', response.content))
        self.assertEqual((retained.status_code, retained.data['runId']), (202, run.run_id))
        self.assertEqual(self.financial_dispatch_state(), before)
        self.config.refresh_from_db()
        self.assertEqual(self.config.pillar_strategy, strategy_before)

    def test_flag_off_retains_no_outbox_command_without_entering_legacy_start(self):
        self.client.raise_request_exception = False
        with patch.object(views, '_charge_roo_points_for_article',
                side_effect=ConnectionError('Synthetic pre-debit process interruption')):
            first = self.post()
        self.assertEqual(first.status_code, 500, getattr(first, 'data', first.content))
        self.config.refresh_from_db()
        self.assertIn(commands.KEY, self.config.pillar_strategy)
        self.assertFalse(ContentFactoryDispatchOutbox.objects.exists())
        before = self.financial_dispatch_state()
        strategy_before = deepcopy(self.config.pillar_strategy)
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False), patch.object(
                views, 'start_article_generation', side_effect=AssertionError('No legacy fallthrough')):
            response = self.post()
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(self.financial_dispatch_state(), before)
        self.config.refresh_from_db()
        self.assertEqual(self.config.pillar_strategy, strategy_before)

        missing_key = deepcopy(self.body)
        missing_key.pop('client_request_id')
        for enabled in (True, False):
            with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=enabled), patch.object(
                    views, 'start_article_generation', side_effect=AssertionError('No known-header legacy fallthrough')):
                response = self.post(body=missing_key)
            self.assertEqual(response.status_code, 409, getattr(response, 'data', response.content))
            self.assertEqual(self.financial_dispatch_state(), before)
            self.config.refresh_from_db()
            self.assertEqual(self.config.pillar_strategy, strategy_before)
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False), patch.object(
                views, 'start_article_generation', side_effect=AssertionError('No enrolled keyed legacy fallthrough')):
            response = self.client.post(self.url, missing_key, format='json',
                HTTP_IDEMPOTENCY_KEY='unseen-header-with-original-no-outbox-claim')
        self.assertEqual(response.status_code, 409, getattr(response, 'data', response.content))
        self.assertEqual(self.financial_dispatch_state(), before)
        self.config.refresh_from_db()
        self.assertEqual(self.config.pillar_strategy, strategy_before)

    def test_retained_header_cannot_drop_or_change_original_body_key_on_or_off(self):
        self.start()
        self.client.raise_request_exception = False
        before = self.financial_dispatch_state()
        self.config.refresh_from_db()
        strategy_before = deepcopy(self.config.pillar_strategy)
        for enabled in (True, False):
            for mode in ('missing', 'empty', 'changed'):
                with self.subTest(enabled=enabled, mode=mode):
                    body = deepcopy(self.body)
                    body.pop('client_request_id')
                    if mode != 'missing':
                        body['client_request_id'] = '' if mode == 'empty' else str(uuid4())
                    with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=enabled):
                        response = self.post(body=body)
                    self.assertEqual(response.status_code, 409, getattr(response, 'data', response.content))
                    self.assertEqual(self.financial_dispatch_state(), before)
        self.config.refresh_from_db()
        self.assertEqual(self.config.pillar_strategy, strategy_before)

    def test_unseen_header_without_body_key_keeps_legacy_delegation_with_an_enrolled_namespace(self):
        from rest_framework.response import Response
        self.start()
        body = deepcopy(self.body)
        body.pop('client_request_id')
        before = self.financial_dispatch_state()
        for enabled, header in ((True, 'genuinely-unseen-header-only-legacy'), (False, None)):
            with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=enabled), patch.object(
                    views, 'start_article_generation', return_value=Response({'legacy': True}, status=202)) as legacy:
                headers = {'HTTP_IDEMPOTENCY_KEY': header} if header is not None else {}
                response = self.client.post(self.url, body, format='json', **headers)
            self.assertEqual((response.status_code, response.json()), (202, {'legacy': True}))
            legacy.assert_called_once()
            self.assertEqual(self.financial_dispatch_state(), before)

    def test_enrolled_flag_off_refuses_unseen_header_when_body_key_is_missing(self):
        self.start()
        self.client.raise_request_exception = False
        body = deepcopy(self.body)
        body.pop('client_request_id')
        before = self.financial_dispatch_state()
        self.config.refresh_from_db()
        strategy_before = deepcopy(self.config.pillar_strategy)
        config_before = type(self.config).objects.values().get(pk=self.config.pk)
        for header in ('unseen-paid-header-while-disabled', ''):
            with self.subTest(header=header), override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
                response = self.client.post(self.url, body, format='json', HTTP_IDEMPOTENCY_KEY=header)
                self.assertEqual(response.status_code, 409, getattr(response, 'data', response.content))
                self.assertEqual(self.financial_dispatch_state(), before)
                self.config.refresh_from_db()
                self.assertEqual(self.config.pillar_strategy, strategy_before)
                self.assertEqual(type(self.config).objects.values().get(pk=self.config.pk), config_before)

    def test_unenrolled_flag_off_header_only_http_keeps_legacy_delegation(self):
        from rest_framework.response import Response
        body = deepcopy(self.body)
        body.pop('client_request_id')
        before = self.financial_dispatch_state()
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False), patch.object(
                views, 'start_article_generation', return_value=Response({'legacy': True}, status=202)) as legacy:
            response = self.client.post(self.url, body, format='json', HTTP_IDEMPOTENCY_KEY='legacy-only-header')
        self.assertEqual((response.status_code, response.json()), (202, {'legacy': True}))
        legacy.assert_called_once()
        self.assertEqual(self.financial_dispatch_state(), before)
        self.config.refresh_from_db()
        self.assertNotIn(commands.KEY, self.config.pillar_strategy)

    def test_protected_outbox_or_malformed_namespace_keeps_known_header_out_of_legacy(self):
        self.start()
        self.config.refresh_from_db()
        original = deepcopy(self.config.pillar_strategy)
        body = deepcopy(self.body)
        body.pop('client_request_id')
        for malformed in (False, True):
            strategy = deepcopy(original)
            if malformed:
                strategy[commands.KEY] = ['unverifiable retained command state']
            else:
                strategy.pop(commands.KEY)
            self.config.pillar_strategy = strategy
            self.config.save(update_fields=['pillar_strategy'])
            before = self.financial_dispatch_state()
            with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False), patch.object(
                    views, 'start_article_generation', side_effect=AssertionError('Retained marker forbids legacy')):
                response = self.post(body=body)
            self.assertEqual(response.status_code, 409, response.data)
            self.assertEqual(self.financial_dispatch_state(), before)
            self.config.refresh_from_db()
            self.assertEqual(self.config.pillar_strategy, strategy)

    def test_frozen_or_malformed_current_roster_cannot_replay_a_paid_command(self):
        self.start()
        for roster in [{'version': 1, 'owner_user_id': self.owner.pk, 'frozen': True}, ['invalid']]:
            self.store(startup_team=roster)
            before = self.financial_dispatch_state()
            response = self.post()
            self.assertIn(response.status_code, (403, 404), response.data)
            self.assertEqual(self.financial_dispatch_state(), before)

    def test_other_company_cannot_adopt_original_paid_key(self):
        self.start()
        other = self.make_org()
        other.company.profile = self.company.profile
        other.company.save(update_fields=['profile'])
        body = self.approved_body(other)
        before = self.financial_dispatch_state()
        replay = self.post(body=body, url=self.route(other.company))
        self.assertEqual(replay.status_code, 409, replay.data)
        self.assertEqual(self.financial_dispatch_state(), before)

    def test_original_confirmed_quote_survives_expiry_of_its_discount(self):
        with patch('startup_updates.benefits.has_monthly_update_discount', return_value=True):
            self.body['expectedCostPoints'] = 6
            run = self.start()
        before = self.financial_dispatch_state()
        with patch('startup_updates.benefits.has_monthly_update_discount', return_value=False):
            self.assertEqual(billing.get_content_factory_article_cost_points(self.org.domain, user=self.owner), 12)
            replay = self.post()
        self.assertEqual(replay.status_code, 202, replay.data)
        self.assertEqual(replay.data['runId'], run.run_id)
        self.assertEqual(self.financial_dispatch_state(), before)
