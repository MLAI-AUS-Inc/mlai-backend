"""E10: funnel stages in the approved order, located per organisation.

The 46-organisation fixture mirrors the 18 Nov catalogue's shape: 35 startups
never connected a repository, each stopped at a known step, and 11 did.
"""
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import User
from crm.models import Contact
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from founder_tools.website_leads import LEADS_KEY
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from . import founder_funnel
from .founder_funnel import APPROVED_STAGES, EVENT_KINDS, STAGES, journey_funnel, organization_funnel, stuck_at
from .models import ArticlePublishStatus, OrganizationContentConfig, WrittenArticle
from .website_models import WebsiteConnection, WebsiteConnectionTarget

START = datetime(2026, 10, 15, tzinfo=dt_timezone.utc)
END = START + timedelta(days=30)

# Where each never-connected startup stopped, and how many (35 in all).
NOT_CONNECTED = {"website_entered": 3, "exited_not_on_github": 4, "github_authorised": 24, "repo_selected": 4}
# How far each connected startup got (11 in all); None reached every stage.
CONNECTED = {"scan_complete": 2, "readers_approved": 2, "author_saved": 1, "articles_page_verified": 2,
             "first_article_started": 1, "draft_ready": 1, None: 2}


def _approve_audience(config):
    from .editorial_catalog import approve_catalog, review_payload, update_catalog
    strategy = update_catalog(dict(config.pillar_strategy or {}), {
        "expected_editorial_catalog_version": 0,
        "audience_options": [{"id": "BUILDER", "reader_task": "Show tested work", "allow_no_offer": True}]})
    review = review_payload(strategy)
    config.pillar_strategy = approve_catalog(strategy, {
        "expected_editorial_catalog_version": review["editorial_catalog_version"],
        "entries": review["review_entries"]}, actor_id="1", approved_at=timezone.now())
    config.save(update_fields=["pillar_strategy", "updated_at"])


class FunnelFixture:
    def startup(self, *, domain=None, joined=START + timedelta(days=1)):
        user = User.objects.create_user(email=f"{uuid4().hex[:12]}@synthetic.test")
        User.objects.filter(pk=user.pk).update(date_joined=joined)
        profile = VibeRaisingProfile.objects.create(user=user, role="founder")
        org = Organization.objects.create(name="Synthetic", domain=domain or f"{uuid4().hex[:10]}.synthetic.test")
        VibeRaisingCompany.objects.create(profile=profile, organization=org, name="Synthetic")
        config, _ = OrganizationContentConfig.objects.get_or_create(organization=org)
        return user, org, config

    def lead(self, user, reason="not_on_github"):
        Contact.objects.create(email=user.email, source=Contact.Source.MANUAL, user=user, attributes={LEADS_KEY: {
            f"{reason}:example.test": {"version": 1, "userId": user.pk, "reason": reason, "domain": "example.test",
                                       "hostClass": "no_code", "createdAt": START.isoformat()}}})

    def make_run(self, org, workflow, status="completed", **request):
        return ContentFactoryRun.objects.create(run_id=uuid4().hex, organization=org, domain=org.domain,
                                                workflow=workflow, status=status, run_request=request)

    def build_stopped_at(self, stage):
        """A startup that reached every approved stage before ``stage`` (None: all of them)."""
        if stage == "website_entered":
            return self.startup(domain=f"{uuid4().hex[:8]}.invalid")
        user, org, config = self.startup()
        if stage == "exited_not_on_github":
            self.lead(user)
            return user, org, config
        if stage == "github_authorised":
            return user, org, config
        order = list(APPROVED_STAGES)
        reached = order[:order.index(stage)] if stage else order
        connection = WebsiteConnection.objects.create(
            organization=org, github_repo="example/site" if "repo_selected" in reached else "",
            installation_id="1", branch="main")
        if "scan_complete" in reached:
            self.make_run(org, "repo_scan")
        if "readers_approved" in reached:
            _approve_audience(config)
        if "author_saved" in reached:
            from startup_updates.models import StartupProfile
            StartupProfile.objects.update_or_create(organization=org, defaults={"founder_names": ["Ada"]})
        if "articles_page_verified" in reached:
            WebsiteConnectionTarget.objects.create(connection=connection, target_key="articles",
                                                   generation=connection.generation, verified_at=timezone.now())
        if "first_article_started" in reached:
            ready = "draft_ready" in reached
            self.make_run(org, "confirmed_topic", status="awaiting_approval" if ready else "running")
            if ready:
                ContentFactoryRun.objects.filter(organization=org).filter(workflow="confirmed_topic").update(result={
                    "draft_ready_v1": {"version": 1, "revision": "r1", "at": (START + timedelta(days=2)).isoformat()}})
        if "published" in reached:
            WrittenArticle.objects.create(
                organization=org, title="Live", slug=f"live-{uuid4().hex[:6]}", category="guides",
                primary_keyword="live", publish_status=ArticlePublishStatus.LIVE,
                live_url=f"https://{org.domain}/blog/live", published_at=timezone.now(),
                live_verified_at=timezone.now())
        return user, org, config

    def catalogue(self):
        orgs = {}
        for stage, count in {**NOT_CONNECTED, **CONNECTED}.items():
            orgs[stage] = [self.build_stopped_at(stage) for _ in range(count)]
        return orgs


@override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False)
class ApprovedFunnelTests(FunnelFixture, TestCase):
    def test_the_46_org_catalogue_locates_35_not_connected_at_the_right_step(self):
        orgs = self.catalogue()
        # Pre-connection work is visible (D6): two free website checks and one
        # content-only first article, all before any website was connected.
        for _user, org, _config in orgs["github_authorised"][:2]:
            self.make_run(org, "website_baseline")
        _user, org, _config = orgs["github_authorised"][2]
        self.make_run(org, "confirmed_topic", status="running", delivery_mode="content_only")
        # A connected startup whose articles live in a CMS left a C15 lead.
        user, _org, _config = orgs["scan_complete"][0]
        self.lead(user, reason="cms_content")

        report = organization_funnel()
        self.assertEqual(report["order"], list(APPROVED_STAGES))
        self.assertEqual(report["organizations"], 46)
        self.assertEqual(report["notConnected"]["organizations"], 35)
        self.assertEqual({stage: count for stage, count in report["notConnected"]["stuckAt"].items() if count},
                         NOT_CONNECTED)
        expected = {**NOT_CONNECTED, **{stage: count for stage, count in CONNECTED.items() if stage}}
        self.assertEqual({stage: count for stage, count in report["stuckAt"].items() if count}, expected)
        self.assertEqual(report["stages"]["startup_created"]["organizations"], 46)
        self.assertEqual(report["stages"]["website_entered"]["organizations"], 43)
        self.assertEqual(report["stages"]["repo_selected"]["organizations"], 11)
        self.assertEqual(report["stages"]["article_live"]["organizations"], 2)
        self.assertEqual(report["preConnection"], {"website_check_started": 2, "content_only_article_started": 1})
        self.assertEqual(report["leads"], {"not_on_github": 4, "cms_content": 1})
        # The staff view's journey report carries the same approved block for the signup window.
        journey = journey_funnel(since=START, until=END)
        self.assertEqual(journey["approved"], report)
        self.assertEqual(set(journey["stages"]), set(STAGES))

    def test_stuck_at_follows_the_approved_order(self):
        reached = {stage: True for stage in APPROVED_STAGES}
        reached["exited_not_on_github"] = False
        self.assertIsNone(stuck_at(reached))
        reached["readers_approved"] = False
        self.assertEqual(stuck_at(reached), "readers_approved")
        reached = {stage: stage in {"signed_up", "website_entered", "startup_created", "exited_not_on_github"}
                   for stage in APPROVED_STAGES}
        self.assertEqual(stuck_at(reached), "exited_not_on_github")
        reached["exited_not_on_github"] = False
        self.assertEqual(stuck_at(reached), "github_authorised")

    def test_signups_outside_the_window_are_left_out(self):
        self.startup(joined=START - timedelta(days=3))
        self.build_stopped_at("github_authorised")
        self.assertEqual(journey_funnel(since=START, until=END)["approved"]["organizations"], 1)


class ApprovedFunnelEventTests(FunnelFixture, TestCase):
    def test_every_new_kind_is_an_event(self):
        for kind in APPROVED_STAGES + founder_funnel.PRE_CONNECTION_EVENTS + founder_funnel.LEAD_EVENTS:
            self.assertIn(kind, EVENT_KINDS)
        with self.assertRaises(ValueError):
            founder_funnel.emit_event("made_up", source="test")

    @override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=True)
    def test_fact_changes_emit_their_stage(self):
        from . import founder_funnel_events
        with patch.object(founder_funnel_events, "emit_for_organization") as emit, \
                self.captureOnCommitCallbacks(execute=True):
            _user, org, _config = self.startup()
            self.make_run(org, "website_baseline", status="queued")
            self.make_run(org, "confirmed_topic", status="queued", delivery_mode="content_only")
            connection = WebsiteConnection.objects.create(organization=org, github_repo="example/site",
                                                          installation_id="1", branch="main")
            self.make_run(org, "repo_scan")
            WebsiteConnectionTarget.objects.create(connection=connection, target_key="articles",
                                                   generation=connection.generation, verified_at=timezone.now())
        kinds = [call.args[0] for call in emit.call_args_list]
        for kind in ("startup_created", "website_entered", "website_check_started", "content_only_article_started",
                     "github_authorised", "repo_selected", "scan_complete", "articles_page_verified"):
            self.assertIn(kind, kinds)
        self.assertLess(kinds.index("website_check_started"), kinds.index("github_authorised"))

    @override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=True, ARTICLES_FIRST_ARTICLE_REQUIRES_CONNECTED_WEBSITE=True)
    def test_a_cms_content_lead_is_its_own_event_not_the_exit(self):
        from founder_tools.website_leads import record_lead
        user = User.objects.create_user(email=f"{uuid4().hex[:12]}@synthetic.test")
        User.objects.filter(pk=user.pk).update(email_verified_at=timezone.now())
        user.refresh_from_db()
        with patch("content_factory.founder_funnel.emit_event") as emit, self.captureOnCommitCallbacks(execute=True):
            record_lead(user, {"reason": "cms_content", "domain": "example.test", "hostClass": "unknown",
                               "notify": True})
        self.assertEqual([call.args[0] for call in emit.call_args_list], ["cms_content_lead"])
