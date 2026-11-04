"""Independent source-clock probes; later revisions cannot date an unknown first draft."""
from datetime import timedelta
from uuid import uuid4
from unittest.mock import patch
from django.test import TransactionTestCase, override_settings
from workflow_runs.models import ContentFactoryRun
from .tests_founder_funnel_journey import JourneyFunnelTests, START
from .models import OrganizationContentConfig


class FirstDraftSourceReviewTests(TransactionTestCase):
    founder = JourneyFunnelTests.founder
    report = JourneyFunnelTests.report
    def later_revision(self, org, source):
        with patch('django.utils.timezone.now', return_value=START+timedelta(seconds=900)):
            return ContentFactoryRun.objects.create(run_id=uuid4().hex, organization=org, domain=org.domain,
            workflow='article_revision', status='awaiting_approval', run_request={'source_run_id': source.run_id},
            result={'draft_ready_v1': {'version': 1, 'revision': 'amended', 'at': (START+timedelta(seconds=900)).isoformat()}})

    def test_later_revision_cannot_supply_missing_first_draft_time(self):
        with patch('django.utils.timezone.now', return_value=START+timedelta(seconds=30)):
            _, org, source = self.founder(legacy=True)
        self.later_revision(org, source)
        report = self.report()
        self.assertEqual(report['stages']['draft_ready']['users'], 1)
        self.assertEqual(report['timeToFirstDraft'], {'p50Seconds': None, 'p90Seconds': None,
            'sampleSize': 0, 'unknownTimingUsers': 1})

    @override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=True,
                       MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT=START.isoformat())
    def test_later_revision_cannot_retime_sticky_unknown_source(self):
        with patch('django.utils.timezone.now', return_value=START+timedelta(seconds=30)):
            _, org, source = self.founder(legacy=True)
        original = OrganizationContentConfig.objects.get(organization=org).pillar_strategy['founder_funnel']['first_draft']
        self.assertEqual(original['runId'], source.run_id)
        self.assertIsNone(original['at'])
        self.later_revision(org, source)
        saved = OrganizationContentConfig.objects.get(organization=org).pillar_strategy['founder_funnel']['first_draft']
        self.assertEqual(saved['runId'], original['runId'])
        self.assertIsNone(saved['at'])


    @override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=True,
                       MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT=START.isoformat())
    def test_same_run_source_proof_fills_unknown_time_without_mutating_clocks(self):
        from .founder_funnel import remember_first_draft
        with patch('django.utils.timezone.now', return_value=START+timedelta(seconds=30)):
            _, org, source = self.founder(legacy=True)
        config = OrganizationContentConfig.objects.get(organization=org)
        clocks = config.updated_at, source.updated_at
        remember_first_draft(org.pk, run_id=source.run_id, at=START+timedelta(seconds=75))
        config.refresh_from_db(); source.refresh_from_db()
        self.assertEqual((config.updated_at, source.updated_at), clocks)
        self.assertEqual(self.report()['timeToFirstDraft']['p50Seconds'], 75)

    def test_later_unknown_source_cannot_poison_an_earlier_known_delivery(self):
        _, org, source = self.founder(seconds=60)
        with patch('django.utils.timezone.now', return_value=START+timedelta(seconds=300)):
            ContentFactoryRun.objects.create(run_id=uuid4().hex, organization=org, workflow='confirmed_topic', status='completed')
        self.assertEqual(self.report()['timeToFirstDraft']['p50Seconds'], 60)

    @override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=True,
                       MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT=START.isoformat())
    def test_unknown_source_remains_uncertain_after_its_normal_deletion(self):
        from .founder_funnel import has_first_draft
        with patch('django.utils.timezone.now', return_value=START+timedelta(seconds=30)):
            _, org, source = self.founder(legacy=True)
        self.later_revision(org, source)
        source.delete()
        self.assertTrue(has_first_draft(org))
        self.assertEqual(self.report()['timeToFirstDraft']['unknownTimingUsers'], 1)
        self.assertEqual(self.report()['timeToFirstDraft']['sampleSize'], 0)

    @override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=True,
                       MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT=START.isoformat())
    def test_resolving_one_source_keeps_another_possibly_earlier_unknown(self):
        from .founder_funnel import remember_first_draft
        with patch('django.utils.timezone.now', return_value=START+timedelta(seconds=30)):
            _, org, source = self.founder(legacy=True)
        with patch('django.utils.timezone.now', return_value=START+timedelta(seconds=50)):
            other = ContentFactoryRun.objects.create(run_id=uuid4().hex, organization=org, workflow='confirmed_topic', status='completed')
        other.delete()
        remember_first_draft(org.pk, run_id=source.run_id, at=START+timedelta(seconds=300))
        self.assertEqual(self.report()['timeToFirstDraft']['unknownTimingUsers'], 1)
        remember_first_draft(org.pk, run_id=other.run_id, at=START+timedelta(seconds=400))
        self.assertEqual(self.report()['timeToFirstDraft']['p50Seconds'], 300)

    def test_a_later_known_article_at_another_startup_cannot_date_the_same_persons_unknown_draft(self):
        from founder_tools.models import VibeRaisingCompany
        from organizations.models import Organization
        with patch('django.utils.timezone.now', return_value=START+timedelta(seconds=30)):
            user, _, _ = self.founder(legacy=True)
        other = Organization.objects.create(name='Another startup', domain=uuid4().hex+'.synthetic.test')
        VibeRaisingCompany.objects.create(profile=user.vibe_raising_profile, organization=other, name='Another startup')
        ContentFactoryRun.objects.create(run_id=uuid4().hex, organization=other, workflow='confirmed_topic', status='completed',
            result={'draft_ready_v1': {'version': 1, 'revision': 'ready', 'at': (START+timedelta(seconds=900)).isoformat()}})
        self.assertEqual(self.report()['timeToFirstDraft']['sampleSize'], 0)
        self.assertEqual(self.report()['timeToFirstDraft']['unknownTimingUsers'], 1)

    def test_proof_history_cap_preserves_uncertainty_instead_of_inventing_a_clock(self):
        from .founder_funnel import _add_draft_proof, _first_draft_marker, MAX_DRAFT_PROOFS
        proofs = {}
        for index in range(MAX_DRAFT_PROOFS+2):
            _add_draft_proof(proofs, str(index), created_at=START)
        _add_draft_proof(proofs, 'deleted-source-with-no-lower-bound')
        _add_draft_proof(proofs, 'known', at=START+timedelta(seconds=90), created_at=START)
        receipt = _first_draft_marker(proofs)
        self.assertTrue(receipt['unknownOverflow'])
        self.assertIsNone(receipt['at'])
        self.assertLessEqual(len(receipt['proofs']), MAX_DRAFT_PROOFS)


    @override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=True,
                       MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT=(START+timedelta(days=1)).isoformat())
    def test_legacy_organization_and_its_activity_clocks_are_unchanged(self):
        from .founder_funnel import remember_first_draft
        with patch('django.utils.timezone.now', return_value=START):
            _, org, source = self.founder(legacy=True)
        config = OrganizationContentConfig.objects.create(organization=org, pillar_strategy={'legacy': 'keep'})
        clocks = config.updated_at, source.updated_at
        remember_first_draft(org.pk, run_id=source.run_id, at=START+timedelta(seconds=60))
        config.refresh_from_db(); source.refresh_from_db()
        self.assertEqual(config.pillar_strategy, {'legacy': 'keep'})
        self.assertEqual((config.updated_at, source.updated_at), clocks)
