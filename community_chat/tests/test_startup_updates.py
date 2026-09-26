"""Contract and disclosure checks. These tests never create a database."""
from datetime import date, timedelta
import json
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch

from django.core import signing
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.http import Http404
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat.authentication import CommunityChatAccountAuthentication
from community_chat.startups import views
from community_chat.startups.connections import SALT, connect_browser, consume_ticket
from community_chat.startups.presentation import PUBLIC_FIELDS, update_payload
from startup_updates import revisions
from startup_updates.review_policy import manual_validation
from vibe_raising.serializers import VibeRaisingMonthlyUpdateUpsertSerializer


@override_settings(COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED=True)
class StartupFacadeTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = Obj(pk=7, is_authenticated=True)

    def request(self, view, body=None, **kwargs):
        request = self.factory.post('/', body or {}, format='json') if body is not None else self.factory.get('/')
        force_authenticate(request, self.user, token=Obj(pk='session'))
        return view.as_view(throttle_classes=())(request, **kwargs)

    def test_only_chat_accounts_can_enter_every_facade(self):
        for name in ('BootstrapView', 'CompaniesView', 'UpdatesView', 'PublishView', 'CommunityView', 'GenerateView', 'UploadCompleteView'):
            cls = getattr(views, name)
            self.assertEqual(cls.authentication_classes, (CommunityChatAccountAuthentication,))
        response = views.BootstrapView.as_view(throttle_classes=())(self.factory.get('/'))
        self.assertEqual(response.status_code, 401)

    def test_bootstrap_returns_real_users_chat_profile_identifier(self):
        self.user = get_user_model()(email='startup-bootstrap@example.invalid')
        profile = Obj()
        with patch.object(views, 'get_or_create_founder_profile', return_value=profile), \
             patch.object(views, 'FounderProfileSerializer', return_value=Obj(data={'companies': []})):
            response = self.request(views.BootstrapView)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['accountId'], str(self.user.community_chat_profile_id))
        self.assertEqual(response.data['profile'], {'companies': []})
        self.assertEqual(response['Cache-Control'], 'private, no-store')

    def test_no_active_generation_renders_a_defined_json_response(self):
        request = self.factory.get('/', {'company_id': 'owned'})
        force_authenticate(request, self.user, token=Obj(pk='session'))
        with patch.object(views, 'get_object_or_404', return_value=Obj()), \
             patch.object(views.founder.VibeRaisingEmailDraftActiveRunView, 'get', return_value=Response(None)):
            response = views.ActiveRunView.as_view(throttle_classes=())(request)
        response.render()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {'run': None})

    @override_settings(COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED=False)
    def test_disabled_returns_no_private_data(self):
        response = self.request(views.BootstrapView)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response['Cache-Control'], 'private, no-store')

    def test_explicit_company_is_mandatory(self):
        response = self.request(views.GenerateView, {})
        self.assertEqual(response.status_code, 400)
        self.assertIn('companyId', response.data)

    def test_foreign_company_is_hidden(self):
        with patch.object(views, 'get_object_or_404', side_effect=Http404) as lookup:
            response = self.request(views.GenerateView, {'companyId': 'foreign'})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(lookup.call_args.kwargs, {'pk': 'foreign', 'profile__user': self.user})

    def test_run_must_belong_to_chosen_startup(self):
        company = Obj(organization=Obj(domain='owned.example'))
        with patch.object(views, 'get_object_or_404', side_effect=[company, Http404]) as lookup:
            response = self.request(views.CancelView, {'companyId': 'owned'}, run_id='foreign-run')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(lookup.call_args.kwargs['domain'], 'owned.example')
        self.assertEqual(lookup.call_args.kwargs['run_id'], 'foreign-run')

    def test_save_cannot_implicitly_approve(self):
        with patch.object(views, 'get_object_or_404', return_value=Obj()):
            response = self.request(views.UpdatesView, {'companyId': 'owned', 'saveMode': 'ready'})
        self.assertEqual(response.status_code, 400)

    def test_publish_requires_review_acknowledgement(self):
        with patch.object(views, 'get_object_or_404', return_value=Obj()):
            response = self.request(views.PublishView, {'companyId': 'owned'}, update_id=1)
        self.assertEqual(response.status_code, 400)
        self.assertIn('reviewed', response.data)

    def test_community_only_selects_matching_approved_publications(self):
        with patch.object(views.MonthlyUpdateDraft, 'objects') as manager:
            manager.filter.return_value.select_related.return_value.order_by.return_value.__getitem__.return_value = []
            response = self.request(views.CommunityView)
        self.assertEqual(response.status_code, 200)
        filters = manager.filter.call_args.kwargs
        self.assertEqual(filters['published_revision__audience'], 'community')
        self.assertEqual(filters['published_revision__approval__audience_visibility'], ['community'])
        self.assertEqual(filters['published_revision__approval__content_hash'].name, 'published_revision__content_hash')

    def test_community_projection_strips_private_evidence(self):
        dto = {key: None for key in PUBLIC_FIELDS}
        dto.update(summary='Approved summary', metrics={'revenue': '0'},
            metricEvidence={'revenue': {'quality': 'founder_asserted', 'source_provider': 'xero', 'record_ids': ['private']}},
            evidenceSnapshot={'secret': 'evidence'}, manualDocuments=[{'storage_path': 'private'}],
            sourceUrl='https://private.invalid', validation={'notes': 'private'}, financialSnapshot={'secret': 42})
        draft = Obj(published_revision=Obj(), organization=Obj(name='Acme'))
        with patch('community_chat.startups.presentation._serialize_monthly_update', return_value=dto):
            result = update_payload(draft, published=True, community=True)
        self.assertEqual(set(result), set(PUBLIC_FIELDS) | {'startup'})
        self.assertEqual(result['metricEvidence'], {'revenue': {'quality': 'founder_asserted'}})
        self.assertEqual(result['metrics']['revenue'], '0')

    def _metric_review_fixture(self, config=None):
        memo = {'kpi_snapshot': [
            {'metric_key': 'revenue', 'label': 'Revenue', 'unit': 'AUD'},
            {'metric_key': 'monthlyCosts', 'label': 'Costs', 'unit': 'AUD'},
        ]}
        if config is not None:
            memo['display_config'] = config
        revision = Obj(structured_memo=memo, validation={}, snapshot=Obj(payload={}))
        draft = Obj(current_revision=revision, published_revision=revision,
            current_revision_id=1, published_revision_id=None, organization=Obj(name='Acme'))
        dto = {
            'metrics': {'revenue': 'AUD 100', 'monthlyCosts': 'AUD 50'},
            'metricEvidence': {'revenue': {'quality': 'verified'}, 'monthlyCosts': {'quality': 'verified'}},
            'metricHistory': {'revenue': [100], 'monthlyCosts': [50]},
            'financialSnapshot': {'cash': 900},
            'conciseAnalysis': {'grossMargin': 50},
            'progressCharts': [{'series': [{'points': [{'value': 50}]}]}],
            'evidenceSnapshot': {'metrics': [{'key': 'revenue', 'display_value': 'AUD 100'}]},
        }
        return draft, dto

    def test_empty_metric_selection_hides_figures_in_existing_chat_review(self):
        draft, dto = self._metric_review_fixture({'full_metric_keys': [], 'snippet_metric_keys': []})
        with patch('community_chat.startups.presentation._serialize_monthly_update', return_value=dto):
            review = update_payload(draft)
            community = update_payload(draft, community=True)
        for field in ('metrics', 'metricEvidence', 'metricHistory'):
            self.assertEqual(review[field], {})
        for field in ('financialSnapshot', 'conciseAnalysis', 'progressCharts'):
            self.assertIsNone(review[field])
        self.assertEqual(community['metrics'], {})
        self.assertEqual(community['metricEvidence'], {})
        self.assertNotIn('evidenceSnapshot', community)
        # The saved revision and owner-only evidence still support editing.
        self.assertEqual(draft.current_revision.structured_memo['kpi_snapshot'][0]['metric_key'], 'revenue')
        self.assertEqual(review['evidenceSnapshot']['metrics'][0]['display_value'], 'AUD 100')

    def test_selected_metric_is_the_only_chat_review_figure(self):
        draft, dto = self._metric_review_fixture({'fullMetricKeys': ['revenue'], 'snippetMetricKeys': []})
        with patch('community_chat.startups.presentation._serialize_monthly_update', return_value=dto):
            review = update_payload(draft)
        self.assertEqual(review['metrics'], {'revenue': 'AUD 100'})
        self.assertEqual(set(review['metricEvidence']), {'revenue'})
        self.assertEqual(review['metricHistory'], {'revenue': [100]})
        self.assertIsNone(review['financialSnapshot'])
        self.assertIsNone(review['progressCharts'])

    def test_missing_metric_selection_keeps_legacy_chat_display(self):
        draft, dto = self._metric_review_fixture()
        with patch('community_chat.startups.presentation._serialize_monthly_update', return_value=dto):
            review = update_payload(draft)
        self.assertEqual(review['metrics'], {'revenue': 'AUD 100', 'monthlyCosts': 'AUD 50'})
        self.assertEqual(set(review['metricEvidence']), {'revenue', 'monthlyCosts'})
        self.assertEqual(review['metricHistory'], {'revenue': [100], 'monthlyCosts': [50]})
        self.assertEqual(review['financialSnapshot'], {'cash': 900})
        self.assertEqual(review['progressCharts'], [{'series': [{'points': [{'value': 50}]}]}])


class ReviewPolicyTests(SimpleTestCase):
    def test_manual_write_is_explicitly_asserted(self):
        self.assertEqual(manual_validation(None, {'summary': 'Hello'}, None)['groundedness_status'], 'founder_asserted')

    def test_disclosure_only_change_cannot_launder_failed_review(self):
        old = {'summary': 'Unsupported claim'}
        validation = {'groundedness_status': 'failed', 'notes': 'No support'}
        self.assertEqual(manual_validation(old, {**old, 'audienceVisibility': 'community'}, validation), validation)
        self.assertEqual(manual_validation(old, old, {})['groundedness_status'], 'pending')

    def test_human_correction_requires_human_review(self):
        result = manual_validation({'summary': 'Wrong'}, {'summary': 'Correct'}, {'groundedness_status': 'failed'})
        self.assertEqual(result['groundedness_status'], 'founder_asserted')

    def test_metric_blank_clears_while_zero_survives(self):
        serializer = VibeRaisingMonthlyUpdateUpsertSerializer(data={'month': 'August', 'year': 2026, 'metrics': {'revenue': '0', 'monthlyCosts': ''}})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data['metrics'], {'revenue': '0', 'monthlyCosts': None})

    def approve(self, revision, **overrides):
        draft = Obj(current_revision=revision, pk=1, published_revision_id=4, published_at=timezone.now(), first_published_at=None, save=MagicMock())
        body = dict(actor=Obj(pk=7), revision_id=revision.pk, revision_hash=revision.content_hash, audience_visibility=['community'])
        body.update(overrides)
        with patch.object(revisions.MonthlyUpdateDraft, 'objects') as drafts, patch.object(revisions.MonthlyUpdateApproval, 'objects') as approvals:
            drafts.select_for_update.return_value.get.return_value = draft
            approvals.get_or_create.return_value = (Obj(content_hash=revision.content_hash, audience_visibility=['community']), True)
            result = revisions.approve_and_publish.__wrapped__(draft, **body)
        return result

    def revision(self, status='passed'):
        return Obj(pk=5, content_hash='exact-hash', structured_memo={'_audience_visibility': ['community']}, validation={'groundedness_status': status})

    def test_stale_receipt_cannot_publish(self):
        for override in ({'revision_id': 4}, {'revision_hash': 'stale'}, {'audience_visibility': ['just_me']}):
            with self.subTest(override=override), self.assertRaises(revisions.RevisionConflict):
                self.approve(self.revision(), **override)

    def test_unverified_or_failed_content_cannot_publish(self):
        for state in ('pending', 'failed', 'needs_review', ''):
            with self.subTest(state=state), self.assertRaises(ValidationError):
                self.approve(self.revision(state))

    def test_approved_publication_points_to_exact_revision(self):
        revision = self.revision('founder_asserted')
        published = self.approve(revision)
        self.assertIs(published.published_revision, revision)
        self.assertIsNone(published.run)
        published.save.assert_called_once()


@override_settings(COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED=True,
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    COMMUNITY_CHAT_FRONTEND_URL='https://chat.example')
class ConnectionHandoffTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.payload = {'uid': 7, 'session': 'session', 'company': 'owned', 'provider': 'gmail'}

    def test_signed_ticket_is_single_use_and_rejects_tampering(self):
        ticket = signing.dumps(self.payload, salt=SALT)
        self.assertEqual(consume_ticket(ticket), self.payload)
        with self.assertRaises(signing.BadSignature):
            consume_ticket(ticket)
        with self.assertRaises(signing.BadSignature):
            consume_ticket(ticket + 'tampered')

    def test_ticket_expires(self):
        with patch('django.core.signing.time.time', return_value=1):
            ticket = signing.dumps(self.payload, salt=SALT)
        with self.assertRaises(signing.SignatureExpired):
            consume_ticket(ticket)

    def test_revoked_session_cannot_open_connection(self):
        ticket = signing.dumps(self.payload, salt=SALT)
        request = APIRequestFactory().get('/', {'ticket': ticket})
        with patch('community_chat.startups.connections.CommunityChatAccountSession.objects') as sessions, patch('community_chat.startups.connections.connector_connect') as connect:
            sessions.select_related.return_value.filter.return_value.first.return_value = Obj(revoked_at=timezone.now())
            response = connect_browser(request)
        self.assertEqual(response.status_code, 400)
        connect.assert_not_called()

    def test_handoff_uses_signed_company_and_trusted_return_url(self):
        from django.http import HttpResponse
        ticket = signing.dumps(self.payload, salt=SALT)
        request = APIRequestFactory().get('/', {'ticket': ticket, 'company_id': 'attacker', 'next': 'https://evil.invalid'})
        user = Obj(pk=7, is_active=True, auth_version=2)
        session = Obj(revoked_at=None, expires_at=timezone.now() + timedelta(days=1), user=user, auth_version=2)
        with patch('community_chat.startups.connections.CommunityChatAccountSession.objects') as sessions, patch('community_chat.startups.connections.VibeRaisingCompany.objects') as companies, patch('community_chat.startups.connections.connector_connect', return_value=HttpResponse()) as connect:
            sessions.select_related.return_value.filter.return_value.first.return_value = session
            companies.get.return_value = Obj(pk='owned')
            response = connect_browser(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(request.GET['company_id'], 'owned')
        self.assertTrue(request.GET['next'].startswith('https://chat.example/my-startup/connections?company_id=owned'))
        connect.assert_called_once_with(request, 'gmail')


class FrozenManualEvidenceTests(SimpleTestCase):
    def snapshot(self, *, metrics=None, base=None):
        from contextlib import ExitStack
        from startup_updates import services
        profile = Obj(reporting_timezone='UTC', kpi_definitions=[], default_currency='AUD', reporting_config_version=1, stage='seed', short_description='Acme')
        org = Obj(pk=1, name='Acme')
        run = Obj(result={}, pk=1, run_request={'input_sources': ['manual_documents'], 'manual_document_ids': ['doc'], 'manual_summary': 'Founder notes'})
        with ExitStack() as stack:
            profiles = stack.enter_context(patch.object(revisions.StartupProfile, 'objects'))
            profiles.get_or_create.return_value = (profile, False)
            observations = stack.enter_context(patch.object(revisions.StartupMetricObservation, 'objects'))
            observations.filter.return_value.values_list.return_value = []
            observations.filter.return_value.order_by.return_value.filter.return_value = []
            events = stack.enter_context(patch.object(revisions.StartupEvent, 'objects'))
            events.filter.return_value.filter.return_value.filter.return_value.order_by.return_value.values.return_value = []
            stack.enter_context(patch('startup_updates.api_views._run_result_candidates', return_value=[]))
            stack.enter_context(patch.object(services, 'build_monthly_financial_snapshot', return_value={}))
            context = stack.enter_context(patch('startup_updates.source_evidence.frozen_manual_sources', return_value={
                'summary': 'Founder notes', 'documents': [{'id': 'doc', 'filename': 'Notes.txt', 'text': 'Launch evidence', 'status': 'processed', 'parse_notes': '', 'content_hash': revisions.content_hash('Launch evidence')}],
            }))
            drafts = stack.enter_context(patch.object(revisions.MonthlyUpdateDraft, 'objects'))
            drafts.filter.return_value.order_by.return_value.select_related.return_value = []
            snapshots = stack.enter_context(patch.object(revisions.MonthlyEvidenceSnapshot, 'objects'))
            snapshots.get_or_create.return_value = (Obj(), True)
            revisions.capture_snapshot(org, date(2026, 3, 1), run=run, manual_metrics=metrics, base_snapshot=base)
        return snapshots.get_or_create.call_args.kwargs['defaults']['payload'], context, org

    def test_notes_and_extracted_text_are_frozen_and_hashed(self):
        payload, context, org = self.snapshot(metrics={'revenue': 0})
        self.assertEqual(context.call_args.args[0], org)
        manual = payload['manual_sources']
        self.assertEqual(manual['summary'], 'Founder notes')
        self.assertEqual(manual['documents'][0]['text'], 'Launch evidence')
        self.assertEqual(manual['documents'][0]['content_hash'], revisions.content_hash('Launch evidence'))
        self.assertEqual(payload['hash'], revisions.content_hash({key: value for key, value in payload.items() if key != 'hash'}))
        revenue = next(item for item in payload['metrics'] if item['key'] == 'revenue')
        self.assertEqual(revenue['display_value'], '0')
        self.assertEqual(revenue['quality'], 'founder_asserted')

    def test_amending_metrics_keeps_prior_notes_and_clears_explicit_blank(self):
        base, _, _ = self.snapshot(metrics={'revenue': '100'})
        payload, context, _ = self.snapshot(metrics={'revenue': None}, base=Obj(payload=base))
        context.assert_not_called()
        self.assertEqual(payload['manual_sources'], base['manual_sources'])
        self.assertIsNot(payload['manual_sources'], base['manual_sources'])
        self.assertFalse(any(item['key'] == 'revenue' for item in payload['metrics']))
