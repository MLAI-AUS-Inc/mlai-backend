from types import SimpleNamespace

from django.test import TestCase

from content_factory.vibe_marketing_views import _run_belongs_to_context
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun


class RunBelongsToContextTests(TestCase):
    """Phase 3b: run authorization prefers the exact organization FK, falling
    back to the normalized-domain compare only for unscoped (legacy) runs."""

    def setUp(self):
        self.org_a = Organization.objects.create(name="A", domain="a.example")
        self.org_b = Organization.objects.create(name="B", domain="b.example")
        self.ctx_a = SimpleNamespace(organization=self.org_a)

    def test_matches_when_organization_fk_matches(self):
        run = ContentFactoryRun.objects.create(
            run_id="auth-1", workflow="article_generation", domain="a.example"
        )
        self.assertEqual(run.organization_id, self.org_a.id)
        self.assertTrue(_run_belongs_to_context(run, self.ctx_a))

    def test_rejects_when_organization_fk_differs(self):
        run = ContentFactoryRun.objects.create(
            run_id="auth-2", workflow="article_generation", domain="b.example"
        )
        self.assertEqual(run.organization_id, self.org_b.id)
        self.assertFalse(_run_belongs_to_context(run, self.ctx_a))

    def test_domain_fallback_when_run_unscoped(self):
        # No Organization yet → run saved unscoped; domain still authorizes.
        run = ContentFactoryRun.objects.create(
            run_id="auth-3", workflow="article_generation", domain="c.example"
        )
        self.assertIsNone(run.organization_id)
        ctx_c = SimpleNamespace(organization=Organization(domain="c.example"))
        self.assertTrue(_run_belongs_to_context(run, ctx_c))

    def test_domain_fallback_rejects_mismatch(self):
        run = ContentFactoryRun.objects.create(
            run_id="auth-4", workflow="article_generation", domain="d.example"
        )
        self.assertIsNone(run.organization_id)
        self.assertFalse(_run_belongs_to_context(run, self.ctx_a))
