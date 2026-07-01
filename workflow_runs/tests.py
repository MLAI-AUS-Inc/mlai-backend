from django.test import TestCase

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun


class ContentFactoryRunOrganizationResolutionTests(TestCase):
    """Phase 3b: ContentFactoryRun.save() auto-resolves the owning Organization
    from the (unique) domain so the FK is populated at every create/update path."""

    def test_save_resolves_organization_from_normalized_domain(self):
        org = Organization.objects.create(name="Acme", domain="acme.com")
        run = ContentFactoryRun.objects.create(
            run_id="run-1", workflow="article_generation", domain="https://www.acme.com/blog"
        )
        run.refresh_from_db()
        self.assertEqual(run.organization_id, org.id)

    def test_save_leaves_organization_null_when_domain_unknown(self):
        run = ContentFactoryRun.objects.create(
            run_id="run-2", workflow="article_generation", domain="unknown.example"
        )
        run.refresh_from_db()
        self.assertIsNone(run.organization_id)

    def test_organization_backfilled_on_later_save_with_update_fields(self):
        run = ContentFactoryRun.objects.create(
            run_id="run-3", workflow="article_generation", domain="late.example"
        )
        self.assertIsNone(run.organization_id)

        org = Organization.objects.create(name="Late", domain="late.example")
        run.status = "running"
        run.save(update_fields=["status"])  # must still persist the resolved org
        run.refresh_from_db()
        self.assertEqual(run.organization_id, org.id)

    def test_existing_organization_is_not_overwritten(self):
        org_a = Organization.objects.create(name="A", domain="a.example")
        Organization.objects.create(name="B", domain="b.example")
        run = ContentFactoryRun.objects.create(
            run_id="run-4", workflow="article_generation", domain="a.example"
        )
        self.assertEqual(run.organization_id, org_a.id)

        # Domain churn does not move an already-scoped run to another tenant.
        run.domain = "b.example"
        run.save(update_fields=["domain"])
        run.refresh_from_db()
        self.assertEqual(run.organization_id, org_a.id)
