"""Plain-language overview boundaries; this suite never creates a database."""
from contextlib import ExitStack, nullcontext
from datetime import date
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch
from django.test import SimpleTestCase
from django.utils import timezone
from community_chat.startups.presentation import update_payload
from startup_updates import revisions
from startup_updates.review_policy import manual_validation, has_narrative
from vibe_raising.views import _build_manual_structured_memo
from vibe_raising.serializers import VibeRaisingMonthlyUpdateUpsertSerializer


class StartupOverviewTests(SimpleTestCase):
    def test_optional_overview_round_trips_and_blank_explicitly_clears(self):
        for overview in ('We help clinics plan care with less paperwork.', ''):
            serializer = VibeRaisingMonthlyUpdateUpsertSerializer(data={
                'month': 'September', 'year': 2026, 'startupOverview': overview,
            })
            self.assertTrue(serializer.is_valid(), serializer.errors)
            self.assertEqual(_build_manual_structured_memo(serializer.validated_data)['startupOverview'], overview)
        serializer = VibeRaisingMonthlyUpdateUpsertSerializer(data={'month': 'September', 'year': 2026})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertNotIn('startupOverview', serializer.validated_data)
        self.assertNotIn('startupOverview', _build_manual_structured_memo(serializer.validated_data))

    def test_generated_overview_is_bounded_text_and_financial_guard_applies(self):
        from startup_updates.serializers import DraftResultSerializer
        from startup_updates.evidence_contract import validate_generated_metric_claims
        for overview in ({'secret': 'private'}, ['not text'], None, 123, 'x' * 2001):
            serializer = DraftResultSerializer(data={
                'snapshot_id': 1, 'month': '2026-09-01', 'structured_memo': {'startupOverview': overview},
            })
            self.assertFalse(serializer.is_valid())
            self.assertIn('structured_memo', serializer.errors)
        for overview in ('Our product costs AUD 1,000.', 'Revenue is {{metric:revenue}}.', 'We raised $100000.'):
            with self.subTest(overview=overview), self.assertRaises(ValueError):
                validate_generated_metric_claims({'startupOverview': overview})
        validate_generated_metric_claims({'startupOverview': 'We help clinics plan care with less paperwork.'})

    def test_overview_edits_need_review_but_omission_does_not_erase_failed_validation(self):
        previous = {'startupOverview': 'We help clinics.', 'summary': 'A busy month.'}
        failed = {'groundedness_status': 'failed', 'notes': 'Unsupported claim'}
        self.assertEqual(manual_validation(previous, {'summary': 'A busy month.'}, failed), failed)
        for overview in ('We help hospitals.', ''):
            result = manual_validation(previous, {**previous, 'startupOverview': overview}, failed)
            self.assertEqual(result['groundedness_status'], 'founder_asserted')

    def test_generation_context_includes_saved_profile_without_extra_lookup(self):
        from startup_updates.services import build_startup_context_snapshot
        from startup_updates.models import StartupProfile
        profile = StartupProfile(short_description='We help clinics.', problem_solved='Too much paperwork.', target_audience='Clinic staff')
        context = build_startup_context_snapshot(organization=Obj(id=7, domain='acme.example', name='Acme'), profile=profile)
        self.assertEqual(context['short_description'], profile.short_description)
        self.assertEqual(context['problem_solved'], profile.problem_solved)
        self.assertEqual(context['target_audience'], profile.target_audience)

    def test_current_preview_and_publication_use_their_own_overview(self):
        def revision(pk, overview):
            memo = {'_audience_visibility': ['community']}
            if overview is not None:
                memo['startupOverview'] = overview
            return Obj(pk=pk, number=pk, content_hash=f'hash-{pk}', snapshot_id=pk,
                snapshot=Obj(payload={'startup': {'description': 'Live profile must not replace the overview.'}}),
                structured_memo=memo, validation={'groundedness_status': 'passed'}, audience='community')
        original = 'We help clinics plan care. Our fee is AUD 500.'
        draft = Obj(id=1, pk=1, creation_key=None, first_published_at=None, update_date=None, structured_memo={'_month_sequence': 1}, current_revision=revision(3, 'We help hospitals plan care.'), published_revision=revision(2, original),
            current_revision_id=3, published_revision_id=2, month=date(2026, 9, 1), updated_at=timezone.now(),
            published_at=timezone.now(), status='draft', organization=Obj(name='Acme'))
        self.assertEqual(update_payload(draft)['startupOverview'], 'We help hospitals plan care.')
        self.assertEqual(update_payload(draft, community=True)['startupOverview'], 'We help hospitals plan care.')
        self.assertEqual(update_payload(draft, published=True, community=True)['startupOverview'], 'We help clinics plan care.')
        self.assertEqual(draft.published_revision.structured_memo['startupOverview'], original)
        draft.published_revision = revision(2, None)
        self.assertEqual(update_payload(draft, published=True, community=True)['startupOverview'], '')

    def test_overview_is_hashed_retained_for_older_clients_and_blank_is_a_new_revision(self):
        current = Obj(pk=2, validation={'groundedness_status': 'passed'}, structured_memo={'_month_sequence': 1, 'startupOverview': 'We help clinics.'}, content_hash='previous')
        snapshot = Obj(pk=4, organization_id=7, month=date(2026, 9, 1), content_hash='snapshot', payload={'metrics': [], 'events': []})
        draft = Obj(pk=1, organization_id=7, month=snapshot.month, current_revision=current,
            structured_memo={'_month_sequence': 1, **current.structured_memo}, update_date=None, published_revision_id=None, published_at=None, revisions=MagicMock(), save=MagicMock())
        draft.revisions.aggregate.return_value = {'n': 2}
        def save(memo):
            with patch.object(revisions.MonthlyUpdateDraft, 'objects') as drafts, patch.object(revisions.MonthlyUpdateRevision, 'objects') as rows:
                drafts.select_for_update.return_value.get.return_value = draft
                rows.create.side_effect = lambda **kwargs: Obj(**kwargs)
                saved = revisions.save_revision.__wrapped__(draft, memo, snapshot=snapshot, expected_revision=2)
            draft.current_revision = current
            return saved
        retained = save({'summary': 'A busy month.'})
        self.assertEqual(retained.structured_memo['startupOverview'], 'We help clinics.')
        cleared = save({'summary': 'A busy month.', 'startupOverview': ''})
        changed = save({'summary': 'A busy month.', 'startupOverview': 'We help hospitals.'})
        self.assertEqual(cleared.structured_memo['startupOverview'], '')
        self.assertEqual(len({retained.content_hash, cleared.content_hash, changed.content_hash}), 3)
        self.assertEqual(current.structured_memo['startupOverview'], 'We help clinics.')

    def test_editor_results_email_and_markdown_include_overview_before_the_update(self):
        from startup_updates.api_views import _serialize_draft_results_bundle
        from startup_updates.services import render_monthly_update_markdown
        from vibe_raising.views import _serialize_draft_for_form, _serialize_email_draft_month
        overview = 'We help clinics plan care.'
        draft = Obj(id=1, pk=1, creation_key=None, first_published_at=None, published_at=None, published_revision_id=None, update_date=None, month=date(2026, 9, 1), current_revision=None, current_revision_id=None,
            structured_memo={'_month_sequence': 1, 'startupOverview': overview, 'summary': 'Our first clinic tried the service.'})
        bundle = _serialize_draft_results_bundle([draft])
        self.assertEqual(bundle['draft']['startupOverview'], overview)
        self.assertEqual(bundle['current_month']['startupOverview'], overview)
        self.assertEqual(_serialize_draft_for_form(draft)['startupOverview'], overview)
        self.assertEqual(_serialize_email_draft_month(draft)['startupOverview'], overview)
        markdown = render_monthly_update_markdown(draft.structured_memo)
        self.assertLess(markdown.index(overview), markdown.index('Our first clinic tried the service.'))



    def test_overview_is_background_and_cannot_qualify_an_empty_month_for_publication(self):
        self.assertFalse(has_narrative({'startupOverview': 'We help shops plan deliveries.'}))

    def test_public_html_puts_overview_before_summary(self):
        from community_chat.startups.public_views import public_sections
        sections = public_sections({'startupOverview': 'We help shops plan deliveries.', 'summary': 'We tested uploads.'})
        self.assertEqual([item['label'] for item in sections], ['What we do', 'Summary'])

    def generation_request(self, *, overview=None, include_overview=True, active_run=None):
        from vibe_raising import views as founder_views
        org, company, binding = Obj(id=7), Obj(organization=Obj()), Obj(id=2, google_connection_id=None)
        body = {'updateDate': '2026-09-30'}
        if include_overview:
            body['startupOverview'] = overview
        request = Obj(user=Obj(id=1), data=body, query_params={})
        patches = {
            '_get_founder_company_context_or_response': ({'company': company, 'domain': 'acme.example'}, None),
            '_ensure_binding_for_company': (org, None, binding),
            '_resolve_manual_documents_for_request': [], '_get_requested_manual_document_ids': [],
            '_get_requested_manual_summary': 'Monthly evidence', '_get_requested_input_sources': ['manual_documents'],
            '_requested_target_month_from_request': date(2026, 9, 1), 'google_connection_for_org': None,
            'coerce_startup_update_sources_for_gmail_scope': (['manual_documents'], None, {}),
            '_sync_selected_connector_sources_for_draft': {}, 'get_open_startup_update_run': active_run,
            '_build_email_draft_payload': {}, '_should_dispatch_existing_run': False,
            'refresh_startup_update_run_source_context': None,
        }
        with ExitStack() as stack:
            for name, result in patches.items():
                stack.enter_context(patch.object(founder_views, name, return_value=result))
            draft = Obj(pk=1, current_revision_id=None, month=date(2026, 9, 1), update_date=date(2026, 9, 30), save=MagicMock())
            stack.enter_context(patch.object(founder_views.transaction, 'atomic', side_effect=lambda: nullcontext()))
            stack.enter_context(patch('startup_updates.update_identity.resolve_update', return_value=(draft, True)))
            stack.enter_context(patch('startup_updates.update_identity.identity_payload', return_value={}))
            stack.enter_context(patch('startup_updates.activity_scope.run_uses_month_scope', return_value=True))
            stack.enter_context(patch('startup_updates.update_identity.narrative_window', return_value={'start': '2026-09-01T00:00:00Z', 'end': '2026-10-01T00:00:00Z'}))
            create = stack.enter_context(patch.object(founder_views, 'create_startup_update_run', return_value=Obj(run_id='new', run_request={})))
            dispatch = stack.enter_context(patch.object(founder_views, '_dispatch_run_to_valley', return_value=True))
            response = founder_views.VibeRaisingEmailDraftStartView().post(request)
        return response, create, dispatch

    def test_generation_passes_custom_blank_and_omitted_background_separately_from_monthly_notes(self):
        for overview, include in (('We help shops.', True), ('', True), (None, False)):
            with self.subTest(overview=overview):
                response, create, dispatch = self.generation_request(overview=overview, include_overview=include)
                self.assertEqual(response.status_code, 201)
                self.assertEqual(create.call_args.kwargs['startup_overview'], overview)
                self.assertEqual(create.call_args.kwargs['manual_summary'], 'Monthly evidence')
                dispatch.assert_called_once()
        for overview in (None, {}, [], 'x' * 2001):
            response, create, dispatch = self.generation_request(overview=overview)
            self.assertEqual(response.status_code, 400)
            create.assert_not_called()
            dispatch.assert_not_called()

    def test_matching_active_run_deduplicates_and_changed_background_conflicts_without_dispatch(self):
        for overview in ('We help shops.', ''):
            run = Obj(run_id='active', run_request={'update_id': 1, 'startup_overview': overview}, status='running', updated_at=timezone.now())
            response, create, dispatch = self.generation_request(overview=overview, active_run=run)
            self.assertEqual(response.status_code, 200)
            create.assert_not_called()
            dispatch.assert_not_called()
            with self.assertRaises(revisions.RevisionConflict):
                self.generation_request(overview='Different company context.', active_run=run)

    def test_compact_profile_and_custom_overview_are_frozen_outside_monthly_evidence(self):
        from community_chat.tests.test_startup_updates import FrozenManualEvidenceTests
        for overview in ('We help shops.', ''):
            payload, _, _ = FrozenManualEvidenceTests().snapshot(run_context={'startup_overview': overview})
            self.assertEqual(payload['startup']['startup_overview'], overview)
            self.assertEqual(payload['startup']['short_description'], 'Acme')
            self.assertIn('problem_solved', payload['startup'])
            self.assertIn('target_audience', payload['startup'])
            self.assertEqual(payload['manual_sources']['summary'], 'Founder notes')
            self.assertEqual(payload['events'], [])
            changed, _, _ = FrozenManualEvidenceTests().snapshot(metrics={'revenue': '0'}, base=Obj(payload=payload))
            self.assertEqual(changed['startup'], payload['startup'])
            self.assertIsNot(changed['startup'], payload['startup'])

    def test_run_background_remains_frozen_when_profile_changes_during_collection(self):
        from community_chat.tests.test_startup_updates import FrozenManualEvidenceTests
        background = {'company_name': 'Saved name', 'short_description': 'Original description',
            'problem_solved': 'Original problem', 'target_audience': 'Original audience'}
        payload, _, _ = FrozenManualEvidenceTests().snapshot(run_context={'startup_context': background})
        self.assertEqual(payload['startup']['description'], 'Original description')
        self.assertEqual(payload['startup']['name'], 'Saved name')
        self.assertEqual(payload['startup']['problem_solved'], 'Original problem')
        self.assertEqual(payload['startup']['target_audience'], 'Original audience')

    def test_model_sample_replay_accepts_only_synthetic_bounded_narrative(self):
        import json
        import tempfile
        from pathlib import Path
        from scripts.test_pulse_readability_database import load_generated_narrative
        sample = {'source': {'synthetic': True, 'domain': 'routenest.example.invalid'},
            'draft': {'structured_memo': {'startupOverview': 'We help shops.', 'topline': 'We tested uploads.',
                'highlights': [{'text': 'Tested with made-up orders.'}], 'lowlights': ['Incomplete addresses are a problem.']}},
            'generation_model': 'synthetic-model', 'review_model': 'synthetic-reviewer'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.json'
            path.write_text(json.dumps(sample))
            narrative, provenance = load_generated_narrative(path)
            self.assertEqual(narrative['summary'], 'We tested uploads.')
            self.assertEqual(narrative['highlights'], 'Tested with made-up orders.')
            self.assertEqual(narrative['challenges'], 'Incomplete addresses are a problem.')
            self.assertEqual(provenance['generationModel'], 'synthetic-model')
            self.assertEqual(len(provenance['sampleSha256']), 64)
            sample['source']['synthetic'] = False
            path.write_text(json.dumps(sample))
            with self.assertRaises(ValueError):
                load_generated_narrative(path)
            sample['source']['synthetic'] = True
            sample['draft']['structured_memo']['startupOverview'] = 'x' * 2001
            path.write_text(json.dumps(sample))
            with self.assertRaises(ValueError):
                load_generated_narrative(path)
