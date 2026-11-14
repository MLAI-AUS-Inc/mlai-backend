from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun

from .content_islands import seed_islands_from_bootstrap_pillars
from .editorial_catalog import merge_strategy
from .island_selection import selection_runs
from .models import ContentIsland, ContentIslandKeyword, OrganizationContentConfig, ResearchedKeyword, TopicFeedback
from .research_workspace import RESET_KEY, ResearchWorkspaceConflict, current_research_runs, require_current_research, reset_workspace
from .run_ownership import reserve_admitted_run, user_principal
from .service_views import SEOClusterBulkUpsertView, SEOContentIslandBulkSyncView, SEOKeywordBulkUpsertView
from .services.island_refresh_scheduler import dispatch_island_refresh


class FounderFeedbackFenceTests(SimpleTestCase):
    def test_retired_feedback_stops_before_engagement_or_persistence(self):
        from .vibe_marketing_views import VibeMarketingTopicFeedbackView
        organization = SimpleNamespace(pk=7)
        context = SimpleNamespace(organization=organization)
        request = SimpleNamespace(data={"keyword": "Stale idea", "sessionId": "old-run"})
        with patch("content_factory.vibe_marketing_views._resolve_context_or_response", return_value=(context, None)), \
                patch("content_factory.founder_library_commands.applies", return_value=False), \
                patch("content_factory.vibe_marketing_views.transaction.atomic"), \
                patch("content_factory.vibe_marketing_views.Organization.objects") as organizations, \
                patch("content_factory.research_workspace.research_config", return_value="locked-config"), \
                patch("content_factory.research_workspace.accepts_research_write", return_value=False) as accepts, \
                patch("content_factory.vibe_marketing_views.record_topic_feedback") as write:
            response = VibeMarketingTopicFeedbackView().post(request)
        self.assertEqual(response.status_code, 409)
        organizations.select_for_update.return_value.get.assert_called_once_with(pk=7)
        accepts.assert_called_once_with("locked-config", "old-run")
        write.assert_not_called()


@override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=True)
class ResearchWorkspaceResetTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(domain="reset.example.test", name="Reset")
        self.other = Organization.objects.create(domain="other.example.test", name="Other")
        self.config = OrganizationContentConfig.objects.create(
            organization=self.org, github_repo="fixture/site", daily_discovery_enabled=True,
            pillar_strategy={"pillars": [{"name": "Old research"}], "editorial_catalog": {"version": 7}},
        )
        self.keyword = ResearchedKeyword.objects.create(organization=self.org, keyword="Old research")
        ResearchedKeyword.objects.create(organization=self.other, keyword="Other research")
        ContentIsland.objects.create(organization=self.org, slug="old", name="Old research")
        TopicFeedback.objects.create(organization=self.org, keyword="Old idea", keyword_normalized="old idea")
        self.old_run = self.admit()
        self.old_run.status = "completed"
        self.old_run.result["island_research_selection"] = {"selected_ids": ["old"]}
        self.old_run.save(update_fields=["status", "result"])

    def admit(self, run_id=None, *, check_research_workspace=False):
        run_id = run_id or str(uuid4())
        return reserve_admitted_run(
            organization=self.org, principal=user_principal(1, "founder:1"), workflow="auto_discovery",
            payload={"domain": self.org.domain, "client_request_id": run_id}, reserved_run_id=run_id,
            check_research_workspace=check_research_workspace,
        )

    def transition(self, action):
        plan = reset_workspace(self.org)
        return reset_workspace(self.org, action=action, reviewed_digest=plan["digest"])

    def callback(self, view, **data):
        return view().post(SimpleNamespace(data={"domain": self.org.domain, **data}))

    def test_reset_fences_old_writes_and_admits_fresh_research(self):
        self.transition("freeze")
        self.assertEqual(self.callback(SEOKeywordBulkUpsertView, keywords=[{"keyword": "late"}],
                                      session_id=self.old_run.run_id).status_code, 409)
        self.assertEqual(dispatch_island_refresh(self.org, local_date=timezone.localdate())["status"], "skipped")
        self.assertEqual(seed_islands_from_bootstrap_pillars(self.org), [])
        with self.assertRaises(ResearchWorkspaceConflict):
            self.admit()

        self.transition("clear")
        self.config.refresh_from_db()
        self.assertEqual(self.config.pillar_strategy["editorial_catalog"], {"version": 7})
        self.assertNotIn("pillars", self.config.pillar_strategy)
        self.assertEqual(self.config.github_repo, "fixture/site")
        self.assertFalse(self.config.daily_discovery_enabled)
        self.assertEqual(merge_strategy(self.config.pillar_strategy, {RESET_KEY: {"phase": "resumed"}})[RESET_KEY]["phase"], "cleared")
        self.assertEqual(list(ResearchedKeyword.objects.values_list("organization_id", flat=True)), [self.other.pk])
        self.assertFalse(ContentIsland.objects.filter(organization=self.org).exists())
        self.assertFalse(TopicFeedback.objects.filter(organization=self.org).exists())
        self.assertFalse(selection_runs(self.org).exists())
        self.assertTrue(ContentFactoryRun.objects.filter(pk=self.old_run.pk).exists())

        self.transition("resume")
        self.config.refresh_from_db()
        self.assertFalse(current_research_runs(ContentFactoryRun.objects.filter(pk=self.old_run.pk), self.config).exists())
        with self.assertRaises(ResearchWorkspaceConflict):
            require_current_research(self.org, self.old_run)
        self.assertEqual(self.callback(SEOContentIslandBulkSyncView, islands=[], run_id=self.old_run.run_id).status_code, 409)
        self.assertEqual(self.callback(SEOClusterBulkUpsertView, clusters=[]).status_code, 409)
        # A worker mirroring an unknown old run after the cutoff cannot turn it
        # into a new backend admission by giving it a more recent timestamp.
        mirror = ContentFactoryRun.objects.create(
            run_id=str(uuid4()), workflow="auto_discovery", domain=self.org.domain, organization=self.org,
        )
        self.assertEqual(self.callback(SEOKeywordBulkUpsertView, keywords=[{"keyword": "unadmitted"}],
                                      session_id=mirror.run_id).status_code, 409)
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False):
            with self.assertRaises(ResearchWorkspaceConflict):
                self.admit(self.old_run.run_id, check_research_workspace=True)
            fresh = self.admit(check_research_workspace=True)
        self.assertEqual(self.callback(SEOKeywordBulkUpsertView, keywords=[{"keyword": "Fresh idea"}],
                                      session_id=fresh.run_id).status_code, 200)
        self.assertTrue(ResearchedKeyword.objects.filter(organization=self.org, keyword="Fresh idea").exists())

    def test_changed_plan_and_in_flight_runs_fail_closed(self):
        self.transition("freeze")
        reviewed = reset_workspace(self.org)
        ResearchedKeyword.objects.filter(pk=self.keyword.pk).update(volume=30)
        with self.assertRaisesRegex(ValueError, "plan changed"):
            reset_workspace(self.org, action="clear", reviewed_digest=reviewed["digest"])
        ContentFactoryRun.objects.filter(pk=self.old_run.pk).update(status="running")
        with self.assertRaisesRegex(ValueError, "Stop and reconcile"):
            self.transition("clear")
        self.assertTrue(ResearchedKeyword.objects.filter(pk=self.keyword.pk).exists())

    def test_founder_feedback_rejects_retired_and_missing_sessions_after_reset(self):
        from .vibe_marketing_views import VibeMarketingTopicFeedbackView
        self.transition("freeze")
        self.transition("clear")
        context = SimpleNamespace(organization=self.org)

        def feedback(session_id):
            request = SimpleNamespace(data={"keyword": "Old idea", "sessionId": session_id})
            with patch("content_factory.vibe_marketing_views._resolve_context_or_response", return_value=(context, None)), \
                    patch("content_factory.founder_library_commands.applies", return_value=False):
                return VibeMarketingTopicFeedbackView().post(request)

        self.assertEqual(feedback(self.old_run.run_id).status_code, 409)
        self.transition("resume")
        self.assertEqual(feedback(self.old_run.run_id).status_code, 409)
        self.assertEqual(feedback(None).status_code, 409)
        self.assertFalse(TopicFeedback.objects.filter(organization=self.org).exists())
        fresh = self.admit()
        self.assertEqual(feedback(fresh.run_id).status_code, 201)
        self.assertEqual(TopicFeedback.objects.get(organization=self.org).session_id, fresh.run_id)

    def test_cross_company_cascade_is_refused(self):
        other_island = ContentIsland.objects.create(organization=self.other, slug="other", name="Other research")
        ContentIslandKeyword.objects.create(island=other_island, keyword=self.keyword)
        with self.assertRaisesRegex(ValueError, "crosses the reviewed organization"):
            reset_workspace(self.org)
