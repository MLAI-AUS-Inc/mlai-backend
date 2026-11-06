"""Whole actual portable admission/CF fields survive the scoped review HTTP DTO."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from workflow_runs.models import ContentFactoryRun
from . import tests_run_current_review as paired
from .models import OrganizationContentConfig
from .run_ownership import current_execution_ticket, owner_receipt, wire_identity
from .run_verified_delivery import project_verified_delivery, verified_draft_ready


@override_settings(**paired.FLAGS, **paired.ALL_P1_FLAGS_ON, ARTICLES_FREE_FIRST_ARTICLE_ENABLED=True,
    CONTENT_FACTORY_URL='https://cf.fixture.invalid', CONTENT_FACTORY_API_KEY='synthetic-portable-wire-key')
class PortableReviewHttpWireTests(TransactionTestCase):
    fixture_name = 'r3_owned_portable_review_approval.json'
    setUp = paired.CurrentReviewTests.setUp
    reads = paired.CurrentReviewTests.reads

    def test_actual_eight_field_portable_worker_becomes_scoped_http_without_identity_or_body_rewrite(self):
        original = self.fixture['original_backend_fixture']
        self.assertEqual(self.run.run_request, original['run_request'])
        self.assertEqual(wire_identity(owner_receipt(self.run)), original['admission'])
        self.assertEqual(current_execution_ticket(self.run), original['execution'])
        with self.reads():
            project_verified_delivery(self.run.run_id)
        self.run.refresh_from_db()
        self.assertTrue(verified_draft_ready(self.run))
        source = self.fixture['article_review_response']
        self.assertEqual(len(source['fields']), 8)
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        config = OrganizationContentConfig.objects.values().get(organization=self.org)
        with patch('content_factory.vibe_marketing_views.http_client.request',
                return_value=paired.response(source)) as provider, \
                patch('requests.get', side_effect=AssertionError('No extra provider read')):
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(f'/api/v1/my-startup/vibe-marketing/runs/{self.run.run_id}/article-review?company_id={self.company.pk}')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(provider.call_count, 1)
        self.assertFalse(any(row['sql'].lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE ')) for row in queries))
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)
        self.assertEqual(OrganizationContentConfig.objects.values().get(organization=self.org), config)
        envelope = response.data['articleReview']
        self.assertEqual(envelope['runId'], original['run_id'])
        self.assertEqual(envelope['companyId'], str(self.company.pk))
        self.assertEqual(envelope['review']['fields'], source['fields'])
        self.assertEqual(envelope['review']['revision'], source['revision'])
        self.assertEqual(envelope['review']['articleExport']['markdown'], self.fixture['canonical_export']['markdown'])
        self.assertEqual(envelope['review']['articleExport']['revision'], source['revision'])
        self.assertEqual(envelope['review'], {key: value for key, value in response.data.items() if key != 'articleReview'})
        self.assertEqual(envelope['access'], {'canEdit': True, 'canApprove': True})
        target = os.environ.get('FOUNDER_EXPORT_R5_PORTABLE')
        if target:
            frozen = Path(__file__).parent / 'fixtures' / self.fixture_name
            Path(target).write_text(json.dumps({'fixture_version': 1,
                'producer': 'actual PostgreSQL constructors -> unchanged real guarded CF portable worker -> actual scoped backend review HTTP',
                'cf_fixture': self.fixture_name, 'cf_fixture_sha256': hashlib.sha256(frozen.read_bytes()).hexdigest(),
                'original_backend_fixture': deepcopy(original),
                'get': {'status': response.status_code, 'body': deepcopy(response.data)}}, indent=2) + '\n')
