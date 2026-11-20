"""Plan F9: the daily founder-health report and its D12 SLO lines, on named-org fixtures.

Synthetic records shaped like the catalogue's alert golden scenarios (orgs
175, 173, 124, 200, 209, 87 and 30), plus the test org 63, a placeholder org
and Lilia's duplicate 208. No production data. Acceptance: the report's lines
are exactly as expected, and the marked test org is in none of them.
The report is bounded: each section stops at its row limit and says so, and
its own queries do not grow with the number of startups. The scheduler posts
only with ARTICLES_FOUNDER_HEALTH_REPORT_ENABLED on, through a fake client.
"""
from datetime import timedelta
from datetime import timezone as dt_timezone
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat.models import CommunityChatDevice
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep

from . import founder_health_report as health
from . import founder_test_orgs
from .founder_journey_alerts import SCHEDULER_REQUEST_SOURCE
from .models import WrittenArticle
from .website_models import WebsiteConnection, WebsiteConnectionOperation, WebsiteConnectionTarget

MELBOURNE = ZoneInfo("Australia/Melbourne")
CHANNEL = "C0123456789"


class FakeSlack:
    def __init__(self, ok=True):
        self.ok, self.posts = ok, []

    def chat_postMessage(self, **kwargs):  # noqa: N802 - the Slack client's name
        self.posts.append(kwargs)
        return {"ok": self.ok, "ts": "1700000000.000100" if self.ok else ""}


class HealthFixture(TestCase):
    def setUp(self):
        cache.clear()
        # Ten in the morning in Melbourne, a little after every fixture row is written.
        local = timezone.localtime(timezone.now(), MELBOURNE).replace(hour=10, minute=0, second=0, microsecond=0)
        self.now = (local + timedelta(days=1)).astimezone(dt_timezone.utc)

    def at(self, **delta):
        return self.now - timedelta(**delta)

    def founder(self, pk, name, domain, *, joined_days, user=None):
        if user is None:
            user = get_user_model().objects.create_user(email=f"{uuid4().hex[:10]}@synthetic.test")
            get_user_model().objects.filter(pk=user.pk).update(date_joined=self.at(days=joined_days))
        profile, _ = VibeRaisingProfile.objects.get_or_create(user=user, defaults={"role": "founder"})
        org = Organization.objects.create(pk=pk, name=name, domain=domain)
        company = VibeRaisingCompany.objects.create(profile=profile, organization=org, name=name, domain=domain)
        VibeRaisingCompany.objects.filter(pk=company.pk).update(created_at=self.at(days=joined_days))
        return org

    def connect(self, org, *, days, ready_after=None, framework="next"):
        connection_row = WebsiteConnection.objects.create(organization=org, github_repo=f"synthetic/{org.pk}",
                                                          capabilities={"framework": framework})
        WebsiteConnection.objects.filter(pk=connection_row.pk).update(created_at=self.at(days=days))
        if ready_after is not None:
            WebsiteConnectionTarget.objects.create(connection=connection_row, target_key="articles", generation=1,
                                                   verified_at=self.at(days=days) + ready_after)
        return connection_row

    def operation(self, connection_row, *, state, receipt, updated):
        op = WebsiteConnectionOperation.objects.create(connection=connection_row, generation=connection_row.generation,
                                                       idempotency_key=uuid4().hex, action="prepare", state=state,
                                                       receipt=receipt)
        WebsiteConnectionOperation.objects.filter(pk=op.pk).update(created_at=updated, updated_at=updated)
        return op

    def make_run(self, org, *, workflow="confirmed_topic", status="failed", code=None, created, updated=None,
            nightly=False):
        run = ContentFactoryRun.objects.create(
            run_id=uuid4().hex, organization=org, domain=org.domain, workflow=workflow, status=status,
            run_request={"request_source": SCHEDULER_REQUEST_SOURCE} if nightly else {},
            result={"error_code": code} if code else {})
        ContentFactoryRun.objects.filter(pk=run.pk).update(created_at=created, updated_at=updated or created)
        return run

    def staff_fixing(self, since, owner=None):
        record = {"version": 1, "since": since.isoformat(), "deadline_at": (since + timedelta(days=7)).isoformat(),
                  "owner": {"user_id": owner, "at": since.isoformat()} if owner else None, "exit": None,
                  "escalations": {}}
        return {"status": "staff_fixing", "reason_code": "DIRECTORY_REPAIR_EXHAUSTED", "staff_fixing": record}

    def build_catalogue(self):
        staff = get_user_model().objects.create_user(email="owner@staff.synthetic.test", is_staff=True)
        # 175: nine failed nightly refreshes; background work, never a founder's stop. Not connected.
        island = self.founder(175, "Island Refresh Co", "islandrefresh.example.com", joined_days=12)
        for night in range(9):
            self.make_run(island, workflow="island_refresh", code="SELF_HEAL_DEADLINE_REACHED",
                     created=self.at(days=night, hours=2), nightly=True)
        # 173 La Vie: Ready within a day; a draft reached review in 30 minutes; a staff fix with a named owner.
        lavie = self.founder(173, "La Vie", "lavie.example.com.au", joined_days=20)
        lavie_site = self.connect(lavie, days=15, ready_after=timedelta(hours=3), framework="astro")
        draft = self.make_run(lavie, status="approval_required", created=self.at(days=1),
                              updated=self.at(days=1) - timedelta(minutes=-30))
        # Its draft was packaged 30 minutes after the request: the funnel's timed first draft.
        ContentFactoryRunStep.objects.create(run=draft, step_key="package_content_delivery", status="completed",
                                             completed_at=self.at(days=1) + timedelta(minutes=30))
        self.operation(lavie_site, state="needs_attention", receipt=self.staff_fixing(self.at(days=3), owner=staff.pk),
                       updated=self.at(days=3))
        # 124 Pawshare: setup stopped two days ago on a directory gate; nobody owns it; never Ready.
        pawshare = self.founder(124, "Pawshare Club", "pawshare.example.com.au", joined_days=20)
        pawshare_site = self.connect(pawshare, days=12, framework="vite-react")
        self.operation(pawshare_site, state="needs_attention",
                       receipt={"status": "stopped", "reason_code": "DIRECTORY_DEPENDENCY_VALIDATION_FAILED"},
                       updated=self.at(days=2))
        # 200 Maxbiz: one nightly SYNC_FAILED (background), Ready in an hour, an article live.
        maxbiz = self.founder(200, "Maxbiz", "maxbiz.example.com", joined_days=30)
        self.connect(maxbiz, days=10, ready_after=timedelta(hours=1))
        self.make_run(maxbiz, workflow="island_refresh", code="SYNC_FAILED", created=self.at(days=4), nightly=True)
        # Its latest article request took three hours to a draft (the writer's receipt): misses the 2 h SLO.
        slow = self.make_run(maxbiz, status="completed", created=self.at(days=3))
        ContentFactoryRun.objects.filter(pk=slow.pk).update(result={"draft_ready_v1": {
            "version": 1, "revision": "r1", "at": (self.at(days=3) + timedelta(hours=3)).isoformat()}})
        article = WrittenArticle.objects.create(organization=maxbiz, title="Live", slug="live", publish_status="live",
                                                live_verified_at=self.at(days=2))
        WrittenArticle.objects.filter(pk=article.pk).update(created_at=self.at(days=20))
        # 209 Lilia: a workers.dev deployment address, and a look-alike 208 in another account.
        self.founder(209, "Lilia Daily Planner", "lilia-planner.sophia.workers.dev", joined_days=12)
        self.founder(208, "Lilia Daily Planner", "lilia-daily.workers.dev", joined_days=11)
        # 87 Vyavos: its nightly research automation blocked for points (background work, never a
        # founder's stop, so not in the top codes); an article pull request still open.
        vyavos = self.founder(87, "Vyavos", "vyavos.example.com", joined_days=40)
        self.connect(vyavos, days=29, ready_after=timedelta(hours=2))
        self.make_run(vyavos, workflow="island_refresh", status="blocked", code="INSUFFICIENT_ROO_POINTS",
                 created=self.at(days=1, hours=6))
        article = WrittenArticle.objects.create(organization=vyavos, title="Open", slug="open", publish_status="pr_open")
        # A legacy request in review with no draft clock at all: unknown, never counted as a pass.
        self.make_run(vyavos, status="approval_required", created=self.at(days=2))
        WrittenArticle.objects.filter(pk=article.pk).update(created_at=self.at(days=1))
        # 30 Tala Thrive: Ready; six cancelled article runs; one stop recorded without a code three hours ago.
        tala = self.founder(30, "Tala Thrive", "talathrive.example.com", joined_days=50)
        self.connect(tala, days=25, ready_after=timedelta(hours=5))
        for index in range(6):
            self.make_run(tala, status="cancelled", created=self.at(days=5, hours=index))
        self.make_run(tala, status="failed", code="TERMINAL_CODE_MISSING", created=self.at(hours=4), updated=self.at(hours=3))
        # 188: a placeholder address.
        self.founder(188, "Placeholder Co", "startup-188.invalid", joined_days=8)
        # 63 xyz startup: the marked test org, failing every way at once.
        test = self.founder(63, "xyz startup", "xyz-startup.vercel.app", joined_days=5)
        test_site = self.connect(test, days=3)
        self.operation(test_site, state="needs_attention", receipt=self.staff_fixing(self.at(days=9)),
                       updated=self.at(days=2))
        self.make_run(test, status="failed", code="UNCLASSIFIED", created=self.at(days=2), updated=self.at(days=2))
        self.make_run(test, status="approval_required", created=self.at(days=1), updated=self.at(hours=20))
        founder_test_orgs.set_marker(test, marked=True, reason="catalogue test org")


class FounderHealthReportTests(HealthFixture):
    maxDiff = None

    def expected_lines(self):
        day = timezone.localtime(self.now, MELBOURNE).date().isoformat()
        since = (self.now - timedelta(days=90)).date().isoformat()
        return [
            f"Founder health {day} (test organisations excluded: 1)",
            "SLOs",
            "  FAIL 90% of connected founders have setup Ready within 24 h: 80% of 5",
            "  FAIL 95% of first drafts within 2 h of the request: 50% of 2",
            "  FAIL Zero founders stuck over 24 h without a named staff owner: 1",
            "  FAIL Zero generic stops shown (last 24 h): 1",
            "  PASS Every staff fix has a named owner within one working day: 0 overdue",
            "  PASS Every staff fix ends by day 7: 0 past day 7",
            f"Funnel (startups from sign-ups since {since}): 9 startups, 4 not connected",
            "  signed_up: 9 (start), 0 stopped here",
            "  website_entered: 8 (89% of previous), 1 stopped here",
            "  exited_not_on_github: 0 (side exit), 0 stopped here",
            "  startup_created: 9 (112% of previous), 0 stopped here",
            "  github_authorised: 5 (56% of previous), 3 stopped here",
            "  repo_selected: 5 (100% of previous), 0 stopped here",
            "  scan_complete: 0 (0% of previous), 5 stopped here",
            "  readers_approved: 0 (n/a of previous), 0 stopped here",
            "  author_saved: 0 (n/a of previous), 0 stopped here",
            "  articles_page_verified: 4 (n/a of previous), 0 stopped here",
            "  first_article_started: 4 (100% of previous), 0 stopped here",
            "  draft_ready: 3 (75% of previous), 0 stopped here",
            "  published: 1 (33% of previous), 0 stopped here",
            "  article_live: 1 (100% of previous), 0 stopped here",
            "Time to first article (sign-up to first draft): p50 456.5 h, p90 651.0 h; 2 timed, 0 unknown",
            "Setup success (7 days): 0 of 2 preparations Ready, 2 stopped; Ready within 24 h: 4 of 5 connected",
            "Articles (7 days): 3 of 10 requests reached review; first draft within 2 h: 1 of 2 timed, 1 unknown",
            "Publish success (7 days): 1 of 2 merged or live, 1 live",
            "Generic stops shown (24 h): 1 for 1 founders",
            "Top codes by founders affected (7 days): DIRECTORY_DEPENDENCY_VALIDATION_FAILED 1, "
            "DIRECTORY_REPAIR_EXHAUSTED 1, TERMINAL_CODE_MISSING 1",
            "Staff fixing: 1 open, 1 owned, 0 owner overdue, 0 past day 7",
            "  astro: 1, oldest 72.0 h",
            "Stuck over 24 h: 2 founders, 1 without an owner",
            "  La Vie (org 173): prepare DIRECTORY_REPAIR_EXHAUSTED, 72.0 h, owned",
            "  Pawshare Club (org 124): prepare DIRECTORY_DEPENDENCY_VALIDATION_FAILED, 48.0 h, no owner",
            "Duplicate organisations: 0 in one profile, 1 across accounts (staff only)",
            "  name liliadailyplanner: orgs 208, 209",
            "Placeholder domains: 1: Placeholder Co (org 188)",
            "Hosting-builder domains: 2: Lilia Daily Planner (org 208, cloudflare_pages), "
            "Lilia Daily Planner (org 209, cloudflare_pages)",
        ]

    def test_named_org_report_has_the_exact_lines_and_no_test_org(self):
        self.build_catalogue()
        report = health.build_report(now=self.now)
        self.assertEqual(report["lines"], self.expected_lines())
        text = health.render_text(report)
        self.assertNotIn("xyz", text)
        self.assertNotIn("org 63", text)
        self.assertNotIn("UNCLASSIFIED", text)
        self.assertEqual([slo["id"] for slo in report["slos"] if not slo["pass"]],
                         ["setup_ready_24h", "first_draft_2h", "stuck_owned", "no_generic_stops"])

    def test_unmarking_brings_the_test_org_back(self):
        self.build_catalogue()
        founder_test_orgs.set_marker(Organization.objects.get(pk=63), marked=False)
        report = health.build_report(now=self.now)
        self.assertEqual(report["excludedTestOrganizations"], 0)
        self.assertIn("UNCLASSIFIED", health.render_text(report))
        self.assertIn("xyz startup (org 63)", health.render_text(report))

    def test_each_section_stops_at_its_row_limit_and_says_so(self):
        self.build_catalogue()
        report = health.build_report(now=self.now, row_limit=1)
        self.assertTrue(report["stops"]["truncated"])
        self.assertEqual(sum(row["founders"] for row in report["stops"]["topCodes"]), 2)
        self.assertTrue(report["setupReady"]["truncated"])
        self.assertEqual(report["setupReady"]["connected"], 1)
        self.assertTrue(report["startupsTruncated"])
        self.assertTrue(report["lines"][-1].startswith("Row limit 1 reached for: "))

    def test_funnel_is_skipped_not_read_when_the_cohort_has_too_many_runs(self):
        self.build_catalogue()
        with patch.object(health, "FUNNEL_RUN_LIMIT", 3), \
                patch("content_factory.founder_funnel.journey_funnel") as journey:
            report = health.build_report(now=self.now)
        journey.assert_not_called()
        since = (self.now - timedelta(days=90)).date().isoformat()
        # Ten founder article runs (La Vie, Maxbiz and Vyavos one each, Tala seven; never the test
        # org's two) are over the limit of 3.
        self.assertIn(f"Funnel skipped: 10 runs since {since} is over the row limit; "
                      "run manage.py founder_funnel_report for it", report["lines"])
        self.assertFalse([line for line in report["lines"] if line.startswith(("  signed_up", "Time to first"))])

    def test_report_queries_do_not_grow_with_the_number_of_startups(self):
        def count():
            with patch.object(health, "_funnel", return_value={
                    "since": self.now.isoformat(), "organizations": 0, "notConnected": 0, "stages": [],
                    "timeToFirstArticle": {"p50Seconds": None, "p90Seconds": None, "sampleSize": 0,
                                           "unknownTimingUsers": 0}}):
                with CaptureQueriesContext(connection) as queries:
                    health.build_report(now=self.now)
            return len(queries)

        self.build_catalogue()
        small = count()
        for index in range(12):
            org = self.founder(1000 + index, f"Extra {index}", f"extra{index}.example.com", joined_days=3)
            site = self.connect(org, days=5, ready_after=timedelta(hours=1))
            self.operation(site, state="needs_attention", receipt={"reason_code": f"STOP_{index}"},
                           updated=self.at(days=2))
            self.make_run(org, status="failed", code=f"RUN_STOP_{index}", created=self.at(days=2))
        self.assertEqual(count(), small)
        self.assertLess(small, 25)

    def test_command_prints_the_lines_and_writes_nothing(self):
        from io import StringIO
        from django.core.management import call_command
        self.build_catalogue()
        out = StringIO()
        with patch.object(health.timezone, "now", return_value=self.now):
            call_command("founder_health_report", stdout=out)
        self.assertEqual(out.getvalue().strip().splitlines(), self.expected_lines())

    @override_settings(MY_STARTUP_STAFF_CONSOLE_ENABLED=True)
    def test_staff_console_view_returns_the_report_to_staff_only(self):
        from community_chat.staff_console_views import FounderHealthView
        self.build_catalogue()
        founder = get_user_model().objects.create_user(email=f"{uuid4().hex[:10]}@synthetic.test")
        request = APIRequestFactory().get("/")
        force_authenticate(request, user=founder)
        self.assertEqual(FounderHealthView.as_view()(request).status_code, 403)
        staff = get_user_model().objects.create_superuser(email=f"{uuid4().hex}@staff.synthetic.test")
        device = CommunityChatDevice.objects.create(user=staff, public_key="c" * 64, status="verified")
        request = APIRequestFactory().get("/")
        force_authenticate(request, user=staff)
        request.community_chat_public_key, request.community_chat_installation_id = (
            device.public_key, device.installation_id)
        with patch.object(health.timezone, "now", return_value=self.now):
            response = FounderHealthView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["lines"], self.expected_lines())
        self.assertIn("no-store", response["Cache-Control"])


class FounderHealthDeliveryTests(HealthFixture):
    @override_settings(ARTICLES_FOUNDER_HEALTH_REPORT_ENABLED=False, CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID=CHANNEL)
    def test_flag_off_posts_nothing(self):
        slack = FakeSlack()
        self.assertEqual(health.run_founder_health_report_sweep(now=self.now, client=slack), {"status": "disabled"})
        self.assertEqual(slack.posts, [])

    @override_settings(ARTICLES_FOUNDER_HEALTH_REPORT_ENABLED=True, CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID=CHANNEL)
    def test_flag_on_posts_once_a_melbourne_day_to_the_ops_channel(self):
        self.build_catalogue()
        slack = FakeSlack()
        early = timezone.localtime(self.now, MELBOURNE).replace(hour=7)
        self.assertEqual(health.run_founder_health_report_sweep(now=early, client=slack), {"status": "not_yet"})
        result = health.run_founder_health_report_sweep(now=self.now, client=slack)
        self.assertEqual(result, {"status": "posted", "failingSlos": ["setup_ready_24h", "first_draft_2h",
                                                                      "stuck_owned", "no_generic_stops"]})
        self.assertEqual(len(slack.posts), 1)
        self.assertEqual(slack.posts[0]["channel"], CHANNEL)
        self.assertEqual(slack.posts[0]["text"].splitlines()[0],
                         f"Founder health {timezone.localtime(self.now, MELBOURNE).date().isoformat()} "
                         "(test organisations excluded: 1)")
        self.assertNotIn("xyz", slack.posts[0]["text"])
        later = self.now + timedelta(hours=3)
        self.assertEqual(health.run_founder_health_report_sweep(now=later, client=slack), {"status": "already_posted"})
        self.assertEqual(len(slack.posts), 1)

    @override_settings(ARTICLES_FOUNDER_HEALTH_REPORT_ENABLED=True, CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID="")
    def test_no_channel_logs_and_posts_nothing(self):
        slack = FakeSlack()
        result = health.run_founder_health_report_sweep(now=self.now, client=slack)
        self.assertEqual(result["status"], "unconfigured")
        self.assertEqual(slack.posts, [])

    @override_settings(ARTICLES_FOUNDER_HEALTH_REPORT_ENABLED=True, CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID=CHANNEL)
    def test_a_failed_post_is_reported_and_never_raised(self):
        slack = FakeSlack(ok=False)
        statuses = [health.run_founder_health_report_sweep(now=self.now + timedelta(minutes=minute), client=slack)["status"]
                    for minute in range(5)]
        # Three tries a day, then the day is left alone; never raised into the scheduler.
        self.assertEqual(statuses, ["undelivered", "undelivered", "undelivered", "already_posted", "already_posted"])
        self.assertEqual(len(slack.posts), 3)

    @override_settings(ARTICLES_FOUNDER_HEALTH_REPORT_ENABLED=True, CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID="",
                       ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED=False, ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED=False)
    def test_article_flags_warns_when_the_report_is_on_without_a_channel(self):
        from .article_flags import warnings
        self.assertEqual(len(warnings()), 1)
        self.assertIn("ARTICLES_FOUNDER_HEALTH_REPORT_ENABLED is on", warnings()[0])
