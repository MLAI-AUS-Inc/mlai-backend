"""Owned portable cancellation matches CF's canonical empty repository tuple."""
from django.test import TestCase, override_settings
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from .run_ownership import RunOwnershipConflict, bind_admitted_run, reserve_admitted_run, user_principal
from .tests_run_event_authority import FLAGS
from .website_cancellation_retirement import expected_article_cancellation


@override_settings(**FLAGS)
class OwnedCancellationTupleTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Synthetic cancellation tuple', domain='cancel-tuple.example.test')

    def owned(self, key, **extra):
        return bind_admitted_run(reserve_admitted_run(organization=self.org, principal=user_principal(123, 'user:123'),
            workflow='article_generation', payload={'domain': self.org.domain, 'client_request_id': key,
                'delivery_mode': 'content_only', 'delivery_mode_confirmed': True, **extra}), 'worker-' + key)

    def test_original_absent_repository_uses_immutable_canonical_empty_without_request_write(self):
        run = self.owned('portable-tuple')
        self.assertNotIn('github_repo', run.run_request)
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False):
            expected = expected_article_cancellation(run)
        self.assertEqual(expected, {'version': 1, 'run_id': run.run_id, 'goal_id': None,
            'scope': {'github_repo': '', 'client_request_id': 'portable-tuple'}})
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)

    def test_corrupt_canonical_repository_does_not_retarget_a_cancellation(self):
        run = self.owned('corrupt-tuple')
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        run.github_repo = 'foreign/repository'
        with self.assertRaises(RunOwnershipConflict):
            expected_article_cancellation(run)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)

    def test_existing_native_repository_scope_is_preserved(self):
        run = self.owned('native-tuple', github_repo='synthetic/current')
        self.assertEqual(expected_article_cancellation(run)['scope'],
                         {'github_repo': 'synthetic/current', 'client_request_id': 'native-tuple'})

    def test_unmarked_legacy_absent_repository_shape_is_unchanged(self):
        run = ContentFactoryRun.objects.create(run_id='legacy-tuple', organization=self.org, domain=self.org.domain,
            workflow='article_generation', run_request={'client_request_id': 'legacy-key'})
        self.assertEqual(expected_article_cancellation(run)['scope'], {'client_request_id': 'legacy-key'})
