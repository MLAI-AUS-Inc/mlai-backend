from startup_updates.cover_images import WATERCOLOR_ARTWORK, normalize_cover_image, retain_cover_image, generated_cover_image
"""Contract and disclosure checks. These tests never create a database."""
from contextlib import ExitStack, nullcontext
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
from vibe_raising.views import _serialize_monthly_update, _build_manual_structured_memo, _extract_display_config
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
        with patch.object(views, 'approved_updates') as lookup:
            lookup.return_value.order_by.return_value.__getitem__.return_value = []
            response = self.request(views.CommunityView)
        self.assertEqual(response.status_code, 200)
        lookup.assert_called_once_with('community', 'public')

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
        self.assertEqual(result['metricEvidence'], {})
        self.assertEqual(result['metrics'], {})
        self.assertEqual(result['displayConfig'], {'snippetMetricKeys': [], 'fullMetricKeys': []})
        self.assertIsNone(result['financialChart'])

    def test_existing_community_publication_discloses_only_relative_totals(self):
        memo = {'kpi_snapshot': [
            {'metric_key': 'revenue', 'value': 'AUD 4,000', 'unit': 'AUD', 'quality': 'source_reported', 'source_provider': 'xero'},
            {'metric_key': 'monthlyCosts', 'value': 'AUD 2,000', 'unit': 'AUD', 'quality': 'source_reported', 'source_provider': 'xero'},
            {'metric_key': 'venue_invoice', 'value': 'AUD 960', 'unit': 'AUD', 'quality': 'source_reported'},
        ]}
        dto = {'metrics': {'revenue': 'AUD 4,000', 'monthlyCosts': 'AUD 2,000', 'venue_invoice': 'AUD 960'},
            'metricEvidence': {item['metric_key']: item.copy() for item in memo['kpi_snapshot']},
            'displayConfig': {'fullMetricKeys': ['revenue', 'monthlyCosts', 'venue_invoice']},
            'summary': 'We launched. Revenue was AUD 4,000. We hosted 100 members.',
            'highlights': '- We delivered the event.\n- Venue invoice: $960.\n- Volunteers helped.',
            'evidenceSnapshot': {'private': 'AUD 960'}, 'financialSnapshot': {'private': 960}}
        revision = Obj(structured_memo=memo)
        draft = Obj(published_revision=revision, organization=Obj(name='Acme'))
        with patch('community_chat.startups.presentation._serialize_monthly_update', return_value=dto):
            result = update_payload(draft, published=True, community=True)
        self.assertEqual(result['financialChart'], {'revenue': 1.0, 'costs': 0.5})
        self.assertEqual(result['summary'], 'We launched. We hosted 100 members.')
        self.assertEqual(result['highlights'], '- We delivered the event.\n- Volunteers helped.')
        self.assertEqual(result['metrics'], {})
        self.assertEqual(result['metricEvidence'], {})
        self.assertNotIn('financialSnapshot', result)
        self.assertNotIn('evidenceSnapshot', result)
        self.assertEqual(memo['kpi_snapshot'][2]['value'], 'AUD 960')

    def test_owner_keeps_metrics_and_private_source_evidence(self):
        dto = {'metrics': {'venue_invoice': 'AUD 960'}, 'metricEvidence': {}, 'evidenceSnapshot': {'private': 960}}
        revision = Obj(structured_memo={}, validation={'groundedness_status': 'passed'}, snapshot=Obj(payload={}))
        draft = Obj(current_revision=revision, current_revision_id=1, published_revision_id=None, organization=Obj(name='Acme'))
        with patch('community_chat.startups.presentation._serialize_monthly_update', return_value=dto):
            result = update_payload(draft)
        self.assertEqual(result['metrics'], {'venue_invoice': 'AUD 960'})
        self.assertEqual(result['evidenceSnapshot'], {'private': 960})

    def test_shared_founder_serializer_does_not_return_amounts(self):
        memo = {'_audience_visibility': ['community'], 'highlights': ['We launched.', 'Venue invoice: AUD 960.'],
            'kpi_snapshot': [{'metric_key': 'venue_invoice', 'value': 'AUD 960', 'unit': 'AUD', 'quality': 'source_reported'}]}
        revision = Obj(pk=2, number=1, content_hash='exact-hash', snapshot_id=3, snapshot=Obj(payload={'private': 960}),
            structured_memo=memo, validation={'groundedness_status': 'passed'}, audience='community')
        draft = Obj(id=1, published_revision_id=2, current_revision_id=2, published_revision=revision, current_revision=revision,
            month=date(2026, 3, 1), update_date=None, month_sequence=1, creation_key=None, updated_at=timezone.now(), published_at=timezone.now(), status='ready')
        with patch('startup_updates.update_identity.identity_payload', return_value={'updateTitle': 'March update'}):
            shared = _serialize_monthly_update(draft, published=True)
        self.assertEqual(shared['metrics'], {})
        self.assertEqual(shared['highlights'], 'We launched.')
        self.assertNotIn('financialSnapshot', shared)
        self.assertNotIn('evidenceSnapshot', shared)
        with patch('startup_updates.update_identity.identity_payload', return_value={'updateTitle': 'March update'}):
            owner = _serialize_monthly_update(draft, published=True, shared=False)
        self.assertEqual(owner['metrics'], {'venue_invoice': 'AUD 960'})


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
        draft = Obj(current_revision=revision, pk=1, month=timezone.localdate().replace(day=1), published_revision_id=4, published_at=timezone.now(), first_published_at=None, ready_at=None, save=MagicMock())
        body = dict(actor=Obj(pk=7), revision_id=revision.pk, revision_hash=revision.content_hash, audience_visibility=['community'])
        body.update(overrides)
        with patch.object(revisions.MonthlyUpdateDraft, 'objects') as drafts, patch.object(revisions.MonthlyUpdateApproval, 'objects') as approvals:
            drafts.select_for_update.return_value.get.return_value = draft
            approvals.get_or_create.return_value = (Obj(content_hash=revision.content_hash, audience_visibility=['community']), True)
            result = revisions.approve_and_publish.__wrapped__(draft, **body)
        return result

    def revision(self, status='passed'):
        return Obj(pk=5, content_hash='exact-hash', structured_memo={'_audience_visibility': ['community']}, validation={'groundedness_status': status}, snapshot=Obj(payload={'period': {'timezone': 'UTC'}}))

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
        session = Obj(pk='session', revoked_at=None, expires_at=timezone.now() + timedelta(days=1), user=user, auth_version=2)
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


class CoverImageTests(SimpleTestCase):
    def test_all_cover_types_round_trip_through_save_and_shared_projection(self):
        from startup_updates.disclosure import shared_update

        choices = [
            {'kind': 'watercolor', 'artwork': 'workspace'},
            {'kind': 'minimal', 'month': 10},
            {'kind': 'upload', 'url': 'https://media.example/cropped-cover.jpg'},
        ]
        for cover in choices:
            with self.subTest(cover=cover):
                serializer = VibeRaisingMonthlyUpdateUpsertSerializer(data={
                    'month': 'October', 'year': 2026,
                    'displayConfig': {'snippetMetricKeys': ['revenue'], 'fullMetricKeys': ['revenue'], 'coverImage': cover},
                })
                self.assertTrue(serializer.is_valid(), serializer.errors)
                memo = _build_manual_structured_memo(serializer.validated_data)
                self.assertEqual(memo['display_config']['cover_image'], cover)
                display = _extract_display_config(memo)
                self.assertEqual(display['coverImage'], cover)
                shared = shared_update({'displayConfig': display})
                self.assertEqual(shared['displayConfig'], {
                    'snippetMetricKeys': [], 'fullMetricKeys': [], 'coverImage': cover,
                })

    def test_invalid_cover_is_rejected_at_save_boundary(self):
        invalid = [
            {'kind': 'minimal', 'month': 0}, {'kind': 'minimal', 'month': 13},
            {'kind': 'minimal', 'month': True}, {'kind': 'minimal', 'month': '10'},
            {'kind': 'watercolor', 'artwork': '../private'},
            {'kind': 'watercolor', 'artwork': 'not-in-catalog'},
            {'kind': 'upload', 'url': 'data:image/png;base64,private'},
            {'kind': 'upload', 'url': 'blob:https://example.test/temporary'},
            {'kind': 'upload', 'url': 'javascript:alert(1)'},
            {'kind': 'upload', 'url': 'https://name:secret@example.test/private'},
            {'kind': 'upload', 'url': 'https://[invalid'},
            {'kind': 'upload', 'url': 'https://media.example/space here'},
            {'kind': 'unknown'}, None,
        ]
        for cover in invalid:
            with self.subTest(cover=cover):
                serializer = VibeRaisingMonthlyUpdateUpsertSerializer(data={
                    'month': 'October', 'year': 2026, 'displayConfig': {'coverImage': cover},
                })
                self.assertFalse(serializer.is_valid())
                self.assertIn('displayConfig', serializer.errors)

    def test_all_twelve_watercolor_options_are_valid(self):
        self.assertEqual(len(WATERCOLOR_ARTWORK), 12)
        for artwork in WATERCOLOR_ARTWORK:
            cover = {'kind': 'watercolor', 'artwork': artwork}
            self.assertEqual(normalize_cover_image(cover), cover)

    def test_first_generated_cover_retains_default_metric_selection(self):
        cover = {'kind': 'watercolor', 'artwork': 'fern-garden'}
        original = {'kpi_snapshot': [{'metric_key': 'revenue', 'value': '100'}, {'metric_key': 'monthlyCosts', 'value': '40'}]}
        generated = generated_cover_image(original, {'cover_image': cover})
        self.assertNotIn('display_config', original)
        self.assertEqual(_extract_display_config(generated), {
            'coverImage': cover, 'fullMetricKeys': ['revenue', 'monthlyCosts'],
            'snippetMetricKeys': ['revenue', 'monthlyCosts'],
        })

    def test_generate_endpoint_passes_selected_cover_to_run_before_dispatch(self):
        from vibe_raising import views as founder_views

        cover = {'kind': 'watercolor', 'artwork': 'orchard'}
        org, company, binding = Obj(id=7), Obj(organization=Obj()), Obj(google_connection_id=None)
        request = Obj(user=Obj(id=1), data={'displayConfig': {'coverImage': cover}, 'updateDate': '2026-09-30'}, query_params={})
        patches = {
            '_get_founder_company_context_or_response': ({'company': company, 'domain': 'acme.example'}, None),
            '_ensure_binding_for_company': (org, None, binding),
            '_resolve_manual_documents_for_request': [], '_get_requested_manual_document_ids': [],
            '_get_requested_manual_summary': 'Founder notes', '_get_requested_input_sources': ['manual_documents'],
            '_requested_target_month_from_request': date(2026, 9, 1), 'google_connection_for_org': None,
            'coerce_startup_update_sources_for_gmail_scope': (['manual_documents'], None, {}),
            '_sync_selected_connector_sources_for_draft': {}, 'get_open_startup_update_run': None,
            '_monthly_update_drafts_cover_input_sources': False, '_build_email_draft_payload': {},
        }
        with ExitStack() as stack:
            for name, result in patches.items():
                stack.enter_context(patch.object(founder_views, name, return_value=result))
            draft = Obj(pk=1, current_revision_id=None, month=date(2026, 9, 1), update_date=date(2026, 9, 30), save=MagicMock())
            stack.enter_context(patch.object(founder_views.transaction, 'atomic', side_effect=lambda: nullcontext()))
            stack.enter_context(patch('startup_updates.update_identity.resolve_update', return_value=(draft, True)))
            stack.enter_context(patch('startup_updates.update_identity.identity_payload', return_value={}))
            stack.enter_context(patch('startup_updates.update_identity.narrative_window', return_value={'start': '2026-09-01T00:00:00Z', 'end': '2026-10-01T00:00:00Z'}))
            create = stack.enter_context(patch.object(founder_views, 'create_startup_update_run', return_value=Obj(run_id='new', run_request={})))
            dispatch = stack.enter_context(patch.object(founder_views, '_dispatch_run_to_valley', return_value=True))
            response = founder_views.VibeRaisingEmailDraftStartView().post(request)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(create.call_args.kwargs['cover_image'], cover)
        dispatch.assert_called_once()

    def test_new_generation_run_persists_cover_without_provider_or_database_access(self):
        from startup_updates import services

        cover = {'kind': 'minimal', 'month': 9}
        org = Obj(id=7, startup_profile=None, domain='acme.example')
        binding = Obj(id=8, google_connection=None, user=Obj(), organization=org, user_id=1)
        with ExitStack() as stack:
            stack.enter_context(patch('integrations.services.external_connectors.google_connection_for_org', return_value=None))
            stack.enter_context(patch.object(services, 'get_open_startup_update_run', return_value=None))
            stack.enter_context(patch.object(services, 'supersede_conflicting_startup_update_runs'))
            stack.enter_context(patch.object(services, 'build_external_context_for_sources', return_value={}))
            stack.enter_context(patch.object(services, 'reconcile_startup_update_run_source_steps'))
            stack.enter_context(patch.object(services.transaction, 'atomic', side_effect=lambda: nullcontext()))
            runs = stack.enter_context(patch.object(services.ContentFactoryRun, 'objects'))
            services.create_startup_update_run(organization=org, binding=binding, target_month=date(2026, 1, 1),
                input_sources=['manual_documents'], manual_summary='A good month', cover_image=cover)
        request = runs.create.call_args.kwargs['run_request']
        self.assertEqual(request['cover_image'], cover)
        self.assertEqual(request['manual_summary'], 'A good month')

    def test_cover_does_not_disclose_arbitrary_metadata(self):
        from startup_updates.disclosure import shared_update

        cover = {'kind': 'watercolor', 'artwork': 'coast', 'storagePath': 'private', 'evidence': {'amount': 100}}
        self.assertEqual(normalize_cover_image(cover), {'kind': 'watercolor', 'artwork': 'coast'})
        shared = shared_update({'displayConfig': {'coverImage': cover, 'private': 'secret'}})
        self.assertEqual(shared['displayConfig'], {
            'snippetMetricKeys': [], 'fullMetricKeys': [],
            'coverImage': {'kind': 'watercolor', 'artwork': 'coast'},
        })

    def test_regeneration_and_older_clients_keep_existing_cover_without_mutation(self):
        cover = {'kind': 'minimal', 'month': 10}
        previous = {'display_config': {'cover_image': cover, 'full_metric_keys': ['revenue']}, 'summary': 'Old'}
        generated = {'summary': 'New'}
        regenerated = retain_cover_image(generated, previous)
        self.assertEqual(regenerated['display_config'], previous['display_config'])
        older_client = retain_cover_image({'display_config': {'full_metric_keys': ['monthlyCosts']}}, previous)
        self.assertEqual(older_client['display_config'], {'cover_image': cover, 'full_metric_keys': ['monthlyCosts']})
        regenerated['display_config']['cover_image']['month'] = 9
        self.assertEqual(cover['month'], 10)
        self.assertEqual(generated, {'summary': 'New'})

    def test_current_and_published_revisions_return_their_own_cover(self):
        def revision(pk, cover):
            return Obj(pk=pk, number=pk, content_hash=f'hash-{pk}', snapshot_id=pk, snapshot=Obj(payload={}),
                structured_memo={'_month_sequence': 1, 'display_config': {'cover_image': cover}, '_audience_visibility': ['community']},
                validation={'groundedness_status': 'passed'}, audience='community')

        published_cover = {'kind': 'watercolor', 'artwork': 'workspace'}
        current_cover = {'kind': 'upload', 'url': 'https://media.example/new-cover.jpg'}
        draft = Obj(id=1, pk=1, organization_id=7, structured_memo={"_month_sequence": 1}, current_revision=revision(3, current_cover), published_revision=revision(2, published_cover),
            current_revision_id=3, published_revision_id=2, month=date(2026, 10, 1), updated_at=timezone.now(),
            published_at=timezone.now(), status='draft', organization=Obj(name='Acme'), update_date=None, creation_key=None, first_published_at=None, month_sequence=1)
        self.assertEqual(update_payload(draft)['displayConfig']['coverImage'], current_cover)
        self.assertEqual(update_payload(draft, community=True)['displayConfig']['coverImage'], current_cover)
        self.assertEqual(update_payload(draft, published=True, community=True)['displayConfig']['coverImage'], published_cover)

    def test_cover_is_retained_in_regenerated_revision_and_changes_revision_hash(self):
        cover = {'kind': 'watercolor', 'artwork': 'workspace'}
        current = Obj(pk=2, structured_memo={'_month_sequence': 1, 'display_config': {'cover_image': cover}}, validation={'groundedness_status': 'passed'}, content_hash='previous')
        snapshot = Obj(pk=4, organization_id=7, month=date(2026, 10, 1), content_hash='snapshot', payload={'metrics': [], 'events': []})
        draft = Obj(pk=1, organization_id=7, month=snapshot.month, current_revision=current,
            structured_memo=current.structured_memo, update_date=None, month_sequence=1, published_at=None, revisions=MagicMock(), save=MagicMock())
        draft.revisions.aggregate.return_value = {'n': 2}

        def saved_memo(incoming):
            with patch.object(revisions.MonthlyUpdateDraft, 'objects') as drafts, patch.object(revisions.MonthlyUpdateRevision, 'objects') as rows:
                drafts.select_for_update.return_value.get.return_value = draft
                rows.create.side_effect = lambda **kwargs: Obj(**kwargs)
                result = revisions.save_revision.__wrapped__(draft, incoming, snapshot=snapshot, expected_revision=2)
            draft.current_revision = current
            return result

        generated = saved_memo({'summary': 'Generated again'})
        self.assertEqual(generated.structured_memo['display_config']['cover_image'], cover)
        selected = saved_memo({'summary': 'Generated again', 'display_config': {'cover_image': {'kind': 'minimal', 'month': 10}}})
        self.assertNotEqual(generated.content_hash, selected.content_hash)
        self.assertEqual(current.structured_memo['display_config']['cover_image'], cover)
