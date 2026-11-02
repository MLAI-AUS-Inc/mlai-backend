"""Actual read-only scoped HTTP controls, real original ledger/grant evidence."""
from copy import deepcopy
import json
import os
from pathlib import Path
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from roo.models import Ledger
from roo.services import PointsService
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep
from .tests_founder_journey_unit import JourneyFixture, make_run
from .founder_article_controls import article_controls
from .models import OrganizationContentConfig


class FounderArticleControlTests(JourneyFixture, TransactionTestCase):
    def http(self, run, *, user=None, company=None):
        from community_chat.my_startup_journey_views import FounderJourneyView
        request = APIRequestFactory().get('/api/v1/my-startup/journey/', {
            'company_id': str((company or self.company).pk), 'runId': run.run_id})
        force_authenticate(request, user=user or self.owner)
        return FounderJourneyView.as_view()(request)

    def read(self, run):
        before = list(ContentFactoryRun.objects.values())
        configs = list(OrganizationContentConfig.objects.values())
        ledgers = list(Ledger.objects.values())
        with patch('requests.sessions.Session.request', side_effect=AssertionError('GET cannot call a provider')):
            with CaptureQueriesContext(connection) as queries:
                response = self.http(run)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(any(row['sql'].lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE ')) for row in queries))
        self.assertEqual(list(ContentFactoryRun.objects.values()), before)
        self.assertEqual(list(OrganizationContentConfig.objects.values()), configs)
        self.assertEqual(list(Ledger.objects.values()), ledgers)
        value = response.data['article']['controls']
        self.assertEqual(set(value), {'canCancel', 'canDeny', 'cancelCopy', 'denyCopy', 'cancelOutcome'})
        return response.data, value

    def paid(self, *, status='running', written=False, earned=False, owned=False):
        from integrations.services.article_generation import _ledger_reference_id
        key = 'canonical-owner-cancel-paid'
        PointsService.credit_purchased_topup(user=self.owner, delta=50, description='Synthetic cancel fixture funding',
            idempotency_key='canonical-cancel-funding')
        spend, _ = PointsService.spend(user=self.owner, delta=12, source='CONTENT_FACTORY',
            description='Synthetic actual original article debit', created_by_slack_id='mlai_user:' + str(self.owner.pk),
            idempotency_key='content_factory:charge:' + key, reference_type='CONTENT_FACTORY', reference_id=_ledger_reference_id(key))
        request = {'delivery_mode': 'content_only', 'delivery_mode_confirmed': True, 'domain': self.org.domain,
            'client_request_id': key, 'roo_points_action': 'article_generation', 'roo_points_authorized': True,
            'roo_points_cost': 12, 'roo_points_required': 12, 'roo_points_billing_status': 'charged',
            'roo_points_ledger_id': str(spend.pk), 'roo_points_billing_user_id': self.owner.pk}
        result = {'draft_ready_v1': {'version': 1}} if earned else {}
        if owned:
            from .run_ownership import reserve_admitted_run, bind_admitted_run, user_principal
            from .run_event_authority import receive_authoritative_run_events
            from .tests_run_event_authority import batch
            run = bind_admitted_run(reserve_admitted_run(organization=self.org, workflow='article_generation',
                principal=user_principal(self.owner.pk, 'mlai_user:' + str(self.owner.pk)), payload=request), 'controls-owned-worker')
            self.assertEqual(receive_authoritative_run_events(batch(run, [{'status': status}])).status_code, 200)
            run.refresh_from_db()
        else:
            run = make_run(self.org, status=status, request=request, result=result)
        from .run_backend_facts import create_job_facts
        create_job_facts(job_id=run.run_id, domain=self.org.domain, client_request_id=key,
            slack_user_id='mlai_user:' + str(self.owner.pk), request_meta=request,
            billing_status='charged', billing_ledger_id=spend.pk, billing_amount=12)
        if written:
            ContentFactoryRunStep.objects.create(run=run, step_key='write_article', status='running',
                started_at=timezone.now(), display_order=1)
        return run

    def test_actual_http_paid_before_writing_offers_only_conditional_original_points_copy(self):
        run = self.paid()
        payload, controls = self.read(run)
        self.assertTrue(controls['canCancel'])
        self.assertFalse(controls['canDeny'])
        self.assertEqual(controls['cancelOutcome'], {'state': 'refund', 'points': 12, 'freeArticle': False})
        self.assertIn('may be returned', controls['cancelCopy']['body'])
        self.assertIn('after the stop', controls['cancelCopy']['body'])
        self.assertIsNone(controls['denyCopy'])
        target = os.environ.get('FOUNDER_EXPORT_R5_CONTROLS')
        if target:
            Path(target).write_text(json.dumps({'source': 'actual authenticated FounderJourneyView GET, real synthetic original ARTICLE debit, no worker/provider or mutation',
                'companyId': str(self.company.pk), 'runId': run.run_id, 'payload': payload}, indent=2) + '\n')

    def test_actual_http_writing_and_earned_draft_keep_original_charge_with_saved_copy(self):
        run = self.paid(status='awaiting_approval', written=True, earned=True)
        _, controls = self.read(run)
        self.assertTrue(controls['canCancel'])
        self.assertTrue(controls['canDeny'])
        self.assertEqual(controls['cancelOutcome'], {'state': 'kept', 'points': 12, 'freeArticle': False})
        self.assertIn('saved draft stays available', controls['cancelCopy']['body'])
        self.assertEqual(controls['denyCopy']['button'], 'Withdraw approval')
        self.assertIn('confirm any points outcome after the stop', controls['denyCopy']['body'])

    def test_actual_http_free_grant_never_promises_an_immediate_release_or_points(self):
        from .first_article_entitlement import reserve_entitlement, entitled_authorization
        request = {'domain': self.org.domain, 'client_request_id': 'canonical-free-cancel',
            'delivery_mode': 'content_only', 'delivery_mode_confirmed': True, 'topic': 'Retained synthetic article',
            'expected_cost_points': 0}
        grant = reserve_entitlement(organization=self.org, user=self.owner, client_request_id=request['client_request_id'],
            data=request, config=self.built.config)
        self.assertIsNotNone(grant)
        request.update(entitled_authorization(domain=self.org.domain, action='article_generation', ledger=grant))
        run = make_run(self.org, request=request)
        for written in (False, True):
            if written:
                ContentFactoryRunStep.objects.create(run=run, step_key='write_article', status='running', started_at=timezone.now())
            _, controls = self.read(run)
            self.assertTrue(controls['canCancel'])
            self.assertEqual(controls['cancelOutcome'], {'state': 'no_charge', 'points': None, 'freeArticle': True})
            self.assertIn("confirm whether your free article can be used again", controls['cancelCopy']['body'])
        self.assertFalse(Ledger.objects.filter(kind__in=['SPEND', 'REFUND']).exists())

    def test_frozen_invalid_roster_inactive_foreign_and_member_do_not_receive_owner_commands(self):
        from core.models import User
        run = make_run(self.org)
        original = deepcopy(self.built.config.pillar_strategy)
        for roster in ({'version': 1, 'frozen': True, 'owner_user_id': self.owner.pk}, ['invalid']):
            OrganizationContentConfig.objects.filter(organization=self.org).update(pillar_strategy={**original, 'startup_team': roster})
            response = self.http(run)
            self.assertIn(response.status_code, {200, 404}, response.data)
            # Invalid retained ownership may be hidden by the facade before
            # projection; it must never be treated as a command capability.
            if response.status_code == 200:
                self.assertEqual(response.data['article']['controls'], {'canCancel': False, 'canDeny': False,
                    'cancelCopy': None, 'denyCopy': None, 'cancelOutcome': None})
            self.assertFalse(article_controls(organization=self.org, company=self.company, user=self.owner, run=run)['canCancel'])
        OrganizationContentConfig.objects.filter(organization=self.org).update(pillar_strategy=original)
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        response = self.http(run)
        self.assertIn(response.status_code, {200, 403, 404})
        if response.status_code == 200:
            self.assertFalse(response.data['article']['controls']['canCancel'])
        User.objects.filter(pk=self.owner.pk).update(is_active=True)
        other = User.objects.create_user(email='synthetic-control-reader@example.test')
        response = self.http(run, user=other)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(article_controls(organization=self.org, company=self.company, user=other, run=run)['canCancel'])

    def test_terminal_stop_settlement_and_external_publication_are_never_command_offers(self):
        run = make_run(self.org)
        for status, result in [('completed', {}), ('cancelled', {}), ('denied', {}),
                ('running', {'cancellation_reason': 'owner_cancelled'}),
                ('running', {'article_terminal_refund': {'status': 'pending'}}),
                ('running', {'pr_url': 'https://github.com/synthetic/site/pull/1'}),
                ('running', {'run_owner_reconciliation_v1': None})]:
            ContentFactoryRun.objects.filter(pk=run.pk).update(status=status, result=result)
            run.refresh_from_db()
            self.assertFalse(article_controls(organization=self.org, company=self.company, user=self.owner, run=run)['canCancel'])
        ContentFactoryRun.objects.filter(pk=run.pk).update(status='running', workflow='repo_scan', result={})
        run.refresh_from_db()
        self.assertFalse(article_controls(organization=self.org, company=self.company, user=self.owner, run=run)['canCancel'])

    def test_owned_verified_body_without_a_raw_delivery_hint_keeps_original_paid_charge(self):
        from django.test import override_settings
        from .tests_run_event_authority import FLAGS
        from .tests_run_verified_delivery import VerifiedDeliveryTests
        from .run_verified_delivery import project_verified_delivery
        with override_settings(**FLAGS, CONTENT_FACTORY_URL='https://controls.fixture.invalid', CONTENT_FACTORY_API_KEY='synthetic-control-proof'):
            run = self.paid(status='awaiting_approval', owned=True)
            self.body, self.revision = '# Retained paid draft\n\nActual canonical saved body.', 'a' * 64
            self.review = {'revision': self.revision, 'previewPending': False,
                'articleExport': {'version': 1, 'status': 'ready', 'runId': run.run_id,
                    'revision': self.revision, 'format': 'markdown', 'markdown': self.body, 'metadata': {}, 'media': []}}
            evidence = VerifiedDeliveryTests.evidence(self, run)
            import requests
            def reply(value):
                response = requests.Response(); response.status_code = 200
                response._content = json.dumps(value).encode(); response._content_consumed = True
                return response
            with patch('requests.get', side_effect=[reply(evidence), reply(self.review)]):
                project_verified_delivery(run.run_id)
            _, controls = self.read(run)
            self.assertTrue(controls['canCancel'])
            self.assertEqual(controls['cancelOutcome'], {'state': 'kept', 'points': 12, 'freeArticle': False})

    def test_legacy_v1_http_journey_remains_unchanged(self):
        from .founder_journey_testing import make_named_legacy_org
        old = make_named_legacy_org('la_vie')
        run = make_run(old.organization)
        response = self.http(run, user=old.owner, company=old.company)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotEqual(response.data.get('contractVersion'), 2)
        self.assertNotIn('article', response.data)

    def test_owned_typed_writing_without_verified_value_explains_a_hold_until_closed(self):
        from django.test import override_settings
        from .tests_run_event_authority import FLAGS, batch
        from .run_event_authority import receive_authoritative_run_events
        with override_settings(**FLAGS):
            run = self.paid(owned=True)
            response = receive_authoritative_run_events(batch(run, [{'seq': 2, 'state_version': 2,
                'type': 'step_started', 'step': 'write_article', 'attempt': 1}]))
            self.assertEqual(response.status_code, 200, response.data)
            _, controls = self.read(run)
            self.assertTrue(controls['canCancel'])
            self.assertEqual(controls['cancelOutcome'], {'state': 'kept', 'points': 12, 'freeArticle': False})
            self.assertIn('held until the stop and any delivered value are confirmed', controls['cancelCopy']['body'])
