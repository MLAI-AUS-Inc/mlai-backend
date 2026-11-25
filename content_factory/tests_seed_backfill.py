"""Plan E9: the seed keyword backfill is dry-run by default and fills only empty seeds.

Acceptance: an org with empty seeds and a name or context gets seeds (so its
research proceeds); an org with nothing at all is left alone, so research ends
with the founder ask add_seed_topics. No run is written and nothing is sent.
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from content_factory.models import OrganizationContentConfig
from content_factory.seed_backfill import apply_plan, plan_for, real_name
from content_factory.startup_first_profile import fallback_seed_keywords
from organizations.models import Organization
from startup_updates.models import StartupProfile
from workflow_runs.models import ContentFactoryRun


class SeedBackfillTests(TestCase):
    def setUp(self):
        self.named = Organization.objects.create(name="Vyavos", domain="vyavos.example")
        OrganizationContentConfig.objects.create(organization=self.named, company_context="")
        StartupProfile.objects.create(organization=self.named, short_description="Vyavos helps clinics automate patient intake.",
                                      target_audience="Clinic managers")
        self.context_only = Organization.objects.create(name="lilia.workers.dev", domain="lilia.workers.dev")
        OrganizationContentConfig.objects.create(organization=self.context_only,
                                                 company_context="A daily planner for busy parents.")
        self.nothing = Organization.objects.create(name="bare.example", domain="bare.example")
        self.seeded = Organization.objects.create(name="Seeded", domain="seeded.example", seed_keywords=["kept seed"])
        StartupProfile.objects.create(organization=self.seeded, short_description="Something else entirely.")

    def run_command(self, *args):
        out = StringIO()
        call_command("backfill_seed_keywords", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_reports_and_writes_nothing(self):
        output = self.run_command()
        self.assertIn("Dry-run", output)
        self.assertIn(f"org {self.named.pk} (vyavos.example): fill", output)
        self.assertIn(f"org {self.nothing.pk} (bare.example): nothing_to_derive", output)
        self.assertIn("1 with nothing to derive, 1 already seeded", output)
        for org in (self.named, self.context_only, self.nothing):
            org.refresh_from_db()
            self.assertEqual(org.seed_keywords, [])
        self.assertFalse(ContentFactoryRun.objects.exists())

    def test_commit_fills_empty_seeds_from_name_and_context_only(self):
        output = self.run_command("--commit")
        self.assertIn("2 written", output)
        self.named.refresh_from_db()
        self.assertEqual(self.named.seed_keywords, fallback_seed_keywords(
            name="Vyavos", description="Vyavos helps clinics automate patient intake.", audience="Clinic managers"))
        self.assertEqual(self.named.seed_keywords[0], "vyavos")
        self.assertEqual(StartupProfile.objects.get(organization=self.named).positive_keywords, self.named.seed_keywords)
        self.context_only.refresh_from_db()
        self.assertTrue(self.context_only.seed_keywords)
        self.assertNotIn("lilia.workers.dev", self.context_only.seed_keywords)
        self.assertFalse(StartupProfile.objects.filter(organization=self.context_only).exists(),
                         "the backfill never creates a profile row")
        self.nothing.refresh_from_db()
        self.assertEqual(self.nothing.seed_keywords, [], "nothing at all stays empty, so research asks add_seed_topics")
        self.seeded.refresh_from_db()
        self.assertEqual(self.seeded.seed_keywords, ["kept seed"])
        self.assertFalse(ContentFactoryRun.objects.exists())

    def test_a_rerun_is_a_no_op_and_org_filter_scopes_it(self):
        self.run_command("--commit", "--org-id", str(self.named.pk))
        self.context_only.refresh_from_db()
        self.assertEqual(self.context_only.seed_keywords, [])
        before = Organization.objects.get(pk=self.named.pk).seed_keywords
        output = self.run_command("--commit", "--org-id", str(self.named.pk))
        self.assertIn("0 written", output)
        self.assertEqual(Organization.objects.get(pk=self.named.pk).seed_keywords, before)

    def test_apply_never_overwrites_seeds_saved_after_the_plan(self):
        plan = plan_for(self.named)
        Organization.objects.filter(pk=self.named.pk).update(seed_keywords=["founder typed this"])
        self.assertFalse(apply_plan(plan))
        self.assertEqual(Organization.objects.get(pk=self.named.pk).seed_keywords, ["founder typed this"])

    def test_a_host_is_not_a_name(self):
        self.assertEqual(real_name("lilia.workers.dev", "lilia.workers.dev"), "")
        self.assertEqual(real_name("workers.dev", "lilia.workers.dev"), "")
        self.assertEqual(real_name("Lilia Daily Planner", "lilia.workers.dev"), "Lilia Daily Planner")
