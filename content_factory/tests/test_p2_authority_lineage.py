"""Re-pinning source inputs never grants stale hosted publication proof."""
from django.test import SimpleTestCase, override_settings
from content_factory import tests_hosted_postmerge_lineage_unit as fixtures
from content_factory.website_contract import WebsiteAuthorityError


@override_settings(ARTICLES_PUBLISHING_INPUTS_V3_ENABLED=True, ARTICLES_HOSTED_ATTESTATION_ENABLED=True,
    ARTICLES_HOSTED_VERIFICATION_ENABLED=True, ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=['7'], ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
    ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=['7'])
class RepinnedHostedLineageTests(SimpleTestCase):
    def setUp(self):
        self.case = fixtures.HostedPostMergeLineageTests()
        self.case.setUp()

    def receipt(self, before, after):
        return dict(version=1, repins=1, first_from_sha=before, last_to_sha=after,
            history=[dict(from_sha=before, to_sha=after, reason='resume_latest',
                          repin_key='setup-parent:1', at='2026-11-02T00:00:00+00:00')])

    def test_repin_before_owned_merge_retains_current_inputs_and_reverifies(self):
        fixture = self.case.fixture
        current = fixture.source_run.run_request['expected_source_sha']
        fixture.source_run.result['re_pin_v1'] = self.receipt('d' * 40, current)
        self.case.merge()
        child, request = self.case.reserve_request()
        self.assertEqual(request['source_run_id'], fixture.source_run.run_id)
        self.assertEqual(request['expected_source_sha'], fixture.proof['source_sha'])
        self.assertIsNotNone(child)

    def test_receipt_cannot_rewrite_inputs_after_owned_merge_reference(self):
        fixture = self.case.fixture
        self.case.merge()
        previous = fixture.source_run.run_request['expected_source_sha']
        fixture.source_run.run_request['expected_source_sha'] = 'd' * 40
        fixture.source_run.result['re_pin_v1'] = self.receipt(previous, 'd' * 40)
        with self.assertRaises(WebsiteAuthorityError):
            self.case.reserve_request()
