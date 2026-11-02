"""Real review metadata effects preserve event-owned execution history and clocks."""
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep, ContentFactoryRunStepAttempt

from .article_review_callbacks import reconcile_setup_review_outcomes
from .article_review_views import record_review_approval
from .models import VibeMarketingComponentComment
from .run_event_authority import receive_authoritative_run_events
from .run_ownership import bind_admitted_run, reserve_admitted_run, user_principal
from .self_heal_testing import make_p2_org
from .tests_r3_revision_child_admission import FLAGS
from .tests_run_event_authority import batch


@override_settings(**FLAGS)
class ReviewMetadataClockTests(TransactionTestCase):
    def setUp(self):
        self.fixture = make_p2_org()
        self.org, self.user = self.fixture.organization, self.fixture.owner
        self.sequence = 0

    def make_run(self, workflow='article_generation', *, owned=True, source=None):
        self.sequence += 1
        key = 'review-clock-' + str(self.sequence)
        request = {'domain': self.org.domain, 'client_request_id': key,
                   'delivery_mode': 'content_only', 'delivery_mode_confirmed': True}
        if source:
            request.update(source_run_id=source.run_id, feedback_batch_id='batch-1')
        if owned:
            run = reserve_admitted_run(organization=self.org, principal=user_principal(self.user.pk, f'mlai_user:{self.user.pk}'),
                workflow=workflow, payload=request)
            run = bind_admitted_run(run, 'worker-' + key)
            response = receive_authoritative_run_events(batch(run, [{'status': 'running'},
                {'type': 'step_started', 'step': 'write', 'attempt': 1},
                {'type': 'step_done', 'step': 'write', 'attempt': 1, 'status': 'completed'}, {'status': 'completed'}]))
            self.assertEqual(response.status_code, 200, response.data)
            run.refresh_from_db()
        else:
            run = ContentFactoryRun.objects.create(run_id='worker-' + key, organization=self.org, domain=self.org.domain,
                workflow=workflow, status='completed', run_request=request)
        return run

    def result(self, run, **values):
        ContentFactoryRun.objects.filter(pk=run.pk).update(result={**run.result, **values})
        run.refresh_from_db()

    def immutable(self, *runs):
        return {'runs': [{key: value for key, value in ContentFactoryRun.objects.values().get(pk=run.pk).items()
                          if key != 'result'} for run in runs],
                'steps': list(ContentFactoryRunStep.objects.order_by('pk').values()),
                'attempts': list(ContentFactoryRunStepAttempt.objects.order_by('pk').values())}

    def setup_callback(self, owned=True):
        source, child = self.make_run('article_system_setup', owned=owned), self.make_run('article_system_setup', owned=owned)
        comment = VibeMarketingComponentComment.objects.create(run=source, actor=self.user,
            component_id='article', body='Retain the reviewed statement', status='submitted', batch_id='batch-1')
        self.result(source, article_review_updates={'batch-1': {'commentIds': [str(comment.pk)], 'revisionRunId': child.run_id}},
            component_feedback_latest_batch={'id': 'batch-1', 'status': 'running'})
        data = {'review_update_operation_id': 'batch-1', 'feedback_batch_id': 'batch-1', 'source_setup_run_id': source.run_id,
            'comment_outcomes': [{'commentId': str(comment.pk), 'status': 'addressed', 'summary': 'Confirmed review change'}]}
        return source, child, comment, data

    def test_owned_setup_callback_keeps_all_execution_and_step_clocks_while_recording_outcomes(self):
        source, child, comment, data = self.setup_callback()
        before = self.immutable(source, child)
        self.assertTrue(reconcile_setup_review_outcomes(data, child))
        self.assertEqual(self.immutable(source, child), before)
        source.refresh_from_db(); child.refresh_from_db(); comment.refresh_from_db()
        self.assertEqual(source.result['component_feedback_latest_batch']['status'], 'completed')
        self.assertEqual(child.result['component_feedback_latest_batch']['revisionRunId'], child.run_id)
        self.assertEqual(comment.context['reviewOutcome']['status'], 'addressed')

    def test_callback_legacy_keeps_existing_clock_behavior(self):
        source, child, _, data = self.setup_callback(owned=False)
        old = source.updated_at, child.updated_at
        self.assertTrue(reconcile_setup_review_outcomes(data, child))
        source.refresh_from_db(); child.refresh_from_db()
        self.assertGreater(source.updated_at, old[0]); self.assertGreater(child.updated_at, old[1])

    def approval_pair(self, *, owned=True):
        source = self.make_run(owned=owned)
        child = self.make_run('article_revision', source=source, owned=owned)
        self.result(child, comment_outcomes=[])
        return source, child

    def test_owned_explicit_review_approval_and_accepted_source_batch_preserve_original_facts(self):
        source, child = self.approval_pair()
        before = self.immutable(source, child)
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False):
            record_review_approval(child, self.fixture, {'reviewedRevision': 'retained-revision',
                'waivedComments': [], 'acceptDisplayedRevision': True})
        self.assertEqual(self.immutable(source, child), before)
        child.refresh_from_db(); source.refresh_from_db()
        self.assertEqual(child.result['article_review_approval']['revision'], 'retained-revision')
        self.assertEqual(source.result['component_feedback_latest_batch']['status'], 'accepted')

    def test_approval_merges_current_metadata_instead_of_overwriting_a_later_owned_receipt(self):
        run = self.make_run()
        ContentFactoryRun.objects.filter(pk=run.pk).update(result={**run.result, 'separate_current_evidence': {'digest': 'fresh'}})
        before = self.immutable(run)
        record_review_approval(run, self.fixture, {'reviewedRevision': 'retained-revision'})
        run.refresh_from_db()
        self.assertEqual(run.result['separate_current_evidence'], {'digest': 'fresh'})
        self.assertEqual(self.immutable(run), before)

    def test_legacy_explicit_review_approval_keeps_clock_behavior(self):
        run = self.make_run(owned=False); old = run.updated_at
        record_review_approval(run, self.fixture, {'reviewedRevision': 'legacy-revision'})
        run.refresh_from_db()
        self.assertGreater(run.updated_at, old)

    def endpoint(self, child, **changes):
        from .vibe_marketing_views import VibeMarketingRunCommentsAcceptRevisionView
        preview = 'https://synthetic.invalid/review/' + child.run_id
        self.result(child, livePreview={'previewUrl': preview, 'exactRender': True, 'proof': {'commitSha': 'a' * 40}, 'resume_generation': 0},
            article_preview_quality={'status': 'passed', 'preview_url': preview, 'resume_generation': 0, 'inputs_sha256': 'b' * 64})
        request = APIRequestFactory().post('/synthetic/accept-review', {'company_id': str(self.fixture.company.pk),
            'reviewedRunId': child.run_id, 'reviewedPreviewUrl': preview, 'reviewedPreviewRevision': 'a' * 40, **changes}, format='json')
        force_authenticate(request, self.user)
        return VibeMarketingRunCommentsAcceptRevisionView.as_view()(request, run_id=child.run_id)

    def test_actual_owner_accept_endpoint_retains_review_identity_and_original_clocks(self):
        source, child = self.approval_pair(); before = self.immutable(source, child)
        response = self.endpoint(child)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.immutable(source, child), before)
        source.refresh_from_db(); child.refresh_from_db()
        self.assertEqual(source.result['component_feedback_latest_batch']['status'], 'accepted')
        self.assertEqual(child.result['component_feedback_latest_batch']['sourceRunId'], source.run_id)

    def test_actual_accept_endpoint_still_refuses_wrong_preview_identity(self):
        source, child = self.approval_pair(); before = self.immutable(source, child)
        response = self.endpoint(child, reviewedPreviewRevision='c' * 40)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(self.immutable(source, child), before)
        self.assertNotIn('component_feedback_latest_batch', ContentFactoryRun.objects.get(pk=source.pk).result)

    def test_reservation_only_receipt_keeps_legacy_execution_clock_behavior(self):
        with override_settings(ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False):
            run = reserve_admitted_run(organization=self.org,
                principal=user_principal(self.user.pk, f'mlai_user:{self.user.pk}'), workflow='article_generation',
                payload={'domain': self.org.domain, 'client_request_id': 'reservation-only-review'})
        old = run.updated_at
        record_review_approval(run, self.fixture, {'reviewedRevision': 'legacy-execution'})
        run.refresh_from_db()
        self.assertGreater(run.updated_at, old)

    def test_actual_accept_endpoint_preserves_a_different_current_source_batch(self):
        source, child = self.approval_pair()
        self.result(source, component_feedback_latest_batch={'id': 'newer-batch', 'status': 'running'})
        before = self.immutable(source, child)
        response = self.endpoint(child)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.immutable(source, child), before)
        source.refresh_from_db()
        self.assertEqual(source.result['component_feedback_latest_batch'], {'id': 'newer-batch', 'status': 'running'})
