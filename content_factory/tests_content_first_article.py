"""P1.6 and P1.7: the "Your first article" card and its one press (IF-P1-09).

29 Oct 2026 funnel: 0 of 80 new users reached an article. The card always has
a topic and, with a one-line description, an audience, and one press starts a
running copy-ready article. Real ORM, the real editorial catalogue and the real
charge path; Content Factory, prices and provider admission are mocked and no
network IO is allowed.
"""
from datetime import timedelta
from hashlib import sha256
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat import my_startup_journey_views as journey_views
from community_chat.throttles import StartupScopedThrottle
from roo.models import Ledger
from startup_updates.models import StartupProfile
from workflow_runs.models import ContentFactoryRun

from . import content_first_article as cards
from . import first_article_entitlement as first_article
from . import founder_journey as journeys
from . import founder_journey_alerts as alerts
from . import vibe_marketing_views as views
from .dispatch_models import ContentFactoryDispatchOutbox
from .editorial_catalog import review_payload
from .founder_journey_testing import ALL_P1_FLAGS_ON, NAMED_LEGACY_ORGS, make_named_legacy_org, make_new_journey_org
from .models import OrganizationContentConfig

FREE = {"ARTICLES_FREE_FIRST_ARTICLE_ENABLED": True, "ARTICLES_FIRST_ARTICLE_GOAL_ENABLED": False,
        "ARTICLES_FREE_FIRST_ARTICLE_DAILY_CAP": 0, "ARTICLES_FREE_FIRST_ARTICLE_SPIKE_THRESHOLD": 0}
FLAGS = {**ALL_P1_FLAGS_ON, **FREE}
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "first-article"},
          "watt_session": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "first-article-w"}}
ONE_LINER = "Booking software for physiotherapy clinics"


def free_rows(org):
    return Ledger.objects.filter(reference_type=first_article.REFERENCE_TYPE,
                                 idempotency_key__startswith=f"content_factory:first_article:org:{org.pk}:")


class CardFixture:
    """A new-journey startup with a one-line description; every outbound call is mocked."""

    one_liner = ONE_LINER
    domain = None

    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(CACHES=LOCMEM, **FLAGS))
        cache.clear()
        self.addCleanup(cache.clear)
        self.factory = APIRequestFactory()
        self.enterContext(patch("requests.sessions.Session.request",
                                side_effect=AssertionError("Unexpected external HTTP request")))
        self.enterContext(patch("founder_tools.services.reconcile_user_slack_id_from_email"))
        self.enterContext(patch.object(views, "get_content_factory_article_cost_points", return_value=12))
        self.balance = 0
        self.enterContext(patch.object(views, "_roo_points_balance_for_user", side_effect=lambda user: self.balance))
        self.pool = self.enterContext(patch.object(views, "_topic_selection_candidate_pool", return_value=[]))
        self.admission = self.enterContext(patch.object(views, "_validate_article_start_admission"))
        self.debits = []
        self.enterContext(patch.object(views, "charge_content_factory_request_for_user", side_effect=self.debit))
        self.enterContext(patch.object(first_article, "_queue_alert"))
        self.enterContext(patch.object(StartupScopedThrottle, "allow_request", return_value=True))
        self.built = self.make_org(self.domain)
        self.org, self.owner, self.company, self.config = (self.built.organization, self.built.owner,
                                                           self.built.company, self.built.config)

    def make_org(self, domain=None, *, email=None):
        built = make_new_journey_org(domain)
        if email:
            built.owner.email = email
            built.owner.save(update_fields=["email"])
        if self.one_liner:
            StartupProfile.objects.update_or_create(organization=built.organization,
                                                    defaults={"short_description": self.one_liner})
        return built

    def debit(self, **kwargs):
        from integrations.services.article_generation import InsufficientRooPointsError
        self.debits.append(kwargs)
        raise InsufficientRooPointsError({"code": "roo_points_required", "requiredPoints": 12,
                                          "currentBalance": self.balance})

    def strategy(self, org=None):
        config = OrganizationContentConfig.objects.get(organization=org or self.org)
        return config.pillar_strategy or {}

    def store(self, **keys):
        config = OrganizationContentConfig.objects.get(organization=self.org)
        config.pillar_strategy = {**(config.pillar_strategy or {}), **keys}
        config.save(update_fields=["pillar_strategy", "updated_at"])

    def site_profile(self, **values):
        self.store(founder_first_profile={"version": 1, "siteProfile": values})

    def call(self, view, *, method="get", data=None, built=None, user=None):
        built = built or self.built
        request = getattr(self.factory, method)(f"/?company_id={built.company.pk}", data=data or {}, format="json")
        force_authenticate(request, user=user or built.owner, token=None)
        response = view.as_view()(request)
        if hasattr(response, "render"):
            response.render()
        return response

    def card(self, built=None):
        response = self.call(journey_views.FirstArticleCardView, built=built)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return response.data

    def press(self, data=None, built=None, user=None):
        card = self.card(built)
        body = {"topicId": card["topic"]["id"], "expectedCostPoints": 0}
        body.update(data or {})
        return self.call(journey_views.FirstArticleStartView, method="post", data=body, built=built, user=user)

    def runs(self, org=None):
        return ContentFactoryRun.objects.filter(organization=org or self.org)


class CardTests(CardFixture, TestCase):
    def test_reading_the_card_never_creates_a_run_or_calls_content_factory(self):
        with patch.object(views, "_call_content_factory_run_action") as action, \
                patch.object(views, "start_article_generation") as start:
            card = self.card()
            self.card()
        self.assertFalse(self.runs().exists())
        self.assertFalse(ContentFactoryDispatchOutbox.objects.exists())
        self.assertFalse(free_rows(self.org).exists())
        action.assert_not_called()
        start.assert_not_called()
        self.assertEqual((card["version"], card["available"], card["free"], card["pricePoints"]), (1, True, True, 0))
        self.assertEqual(card["label"], "Write my first article · free")
        self.assertEqual(card["profile"]["oneLiner"], ONE_LINER)
        self.assertEqual(card["firstArticle"]["status"], "available")

    def test_with_discovery_off_the_topic_comes_from_the_description_with_stable_ids(self):
        first, second = self.card(), self.card()
        self.assertEqual(first["topic"]["source"], "description")
        self.assertTrue(first["topic"]["title"])
        self.assertEqual(len(first["alternatives"]), 2)
        self.assertEqual([first["topic"], *first["alternatives"]], [second["topic"], *second["alternatives"]])
        ids = [item["id"] for item in [first["topic"], *first["alternatives"]]]
        self.assertEqual(len(set(ids)), 3)
        self.assertIn("booking software for physiotherapy clinics", first["topic"]["title"])

    def test_researched_candidates_come_first(self):
        self.pool.return_value = [
            {"id": "cand-low", "title": "Clinic waitlists explained", "keyword": "clinic waitlist", "volume": 10,
             "opportunityScore": 1},
            {"id": "cand-high", "title": "How to cut physio no-shows", "keyword": "physio no shows", "volume": 900,
             "opportunityScore": 9},
            {"id": "cand-written", "title": "Already done", "alreadyWritten": True, "volume": 9000}]
        card = self.card()
        self.assertEqual((card["topic"]["id"], card["topic"]["source"]), ("cand-high", "research"))
        self.assertEqual(card["alternatives"][0]["id"], "cand-low")
        self.assertEqual(card["alternatives"][1]["source"], "description")

    def test_a_candidate_repeating_an_existing_post_is_dropped(self):
        self.pool.return_value = [
            {"id": "same-title", "title": "How to Cut Physio No-Shows!", "volume": 900, "opportunityScore": 9},
            {"id": "same-slug", "title": "Clinic waitlists explained", "volume": 500, "opportunityScore": 5},
            {"id": "fresh", "title": "Choosing physio booking software", "volume": 100, "opportunityScore": 1}]
        self.site_profile(existingPosts=[
            {"url": f"https://{self.org.domain}/blog/older", "title": "How to cut physio no shows"},
            {"url": f"https://{self.org.domain}/blog/clinic-waitlists-explained/", "title": "Waitlists"}])
        card = self.card()
        ids = [item["id"] for item in [card["topic"], *card["alternatives"]]]
        self.assertEqual(ids[0], "fresh")
        self.assertNotIn("same-title", ids)
        self.assertNotIn("same-slug", ids)

    def test_audience_order_approved_then_draft_then_target_audience_then_inferred(self):
        facts = {"oneLiner": ONE_LINER, "targetAudience": None}
        self.assertEqual(cards.audience_for(self.org, self.config, facts)["status"], "inferred")
        self.assertEqual(cards.audience_for(self.org, self.config, facts)["description"], "physiotherapy clinics")
        target = cards.audience_for(self.org, self.config, {**facts, "targetAudience": "Clinic owners in towns"})
        self.assertEqual((target["status"], target["description"]), ("draft", "Clinic owners in towns"))
        draft = {"catalog_schema_version": 2, "id": "clinic-managers", "name": "Clinic managers",
                 "description": "Managers of busy physio clinics", "reader_task": "Fill the diary", "status": "draft",
                 "allow_no_offer": True, "version": 1, "approved_by": None, "approved_at": None}
        self.store(editorial_catalog={"version": 1, "audience_options": [draft], "cta_options": []})
        config = OrganizationContentConfig.objects.get(organization=self.org)
        chosen = cards.audience_for(self.org, config, facts)
        self.assertEqual((chosen["id"], chosen["status"]), ("clinic-managers", "draft"))

    def test_the_inferred_audience_without_for_reads_people_who_need(self):
        self.assertEqual(cards.infer_audience("Fast invoices"), "People who need fast invoices")
        self.assertEqual(cards.infer_audience("Rostering for aged care homes in regional towns."),
                         "aged care homes in regional towns")
        long_tail = "Software for " + " ".join(["clinic"] * 30)
        inferred = cards.infer_audience(long_tail)
        self.assertLessEqual(len(inferred), 80)
        self.assertFalse(inferred.endswith(" "))
        self.assertIsNone(cards.infer_audience(""))

    def test_an_invalid_org_with_a_description_always_has_an_audience_and_no_customer_ask(self):
        self.org.domain = f"banksia-{uuid4().hex[:6]}.invalid"
        self.org.save(update_fields=["domain"])
        card = self.card()
        self.assertEqual(card["audience"]["status"], "inferred")
        self.assertIsNone(card["callToAction"])
        journey = journeys.project_founder_journey(self.org, user=self.owner, company=self.company)
        self.assertNotEqual((journey["action"] or {}).get("id"), "describe_customer")

    def test_call_to_action_only_on_the_startups_own_https_domain(self):
        for url, expected in ((f"https://{self.org.domain}/book", True), (f"https://app.{self.org.domain}/start", True),
                              (f"http://{self.org.domain}/book", False), ("https://elsewhere.example/book", False),
                              (f"https://{self.org.domain}.evil.example/book", False)):
            with self.subTest(url=url):
                self.site_profile(primaryCallToAction={"label": "Book a demo", "url": url})
                cta = self.card()["callToAction"]
                self.assertEqual(cta, {"label": "Book a demo", "url": url} if expected else None)


class StartTests(CardFixture, TestCase):
    def assertStarted(self, response):
        self.assertEqual(response.status_code, 201, response.data)
        run = ContentFactoryRun.objects.get(organization=self.org, run_id=response.data["runId"])
        self.assertEqual(response.data["redirect"],
                         f"/my-startup/runs/{run.run_id}/writing?company_id={self.company.pk}")
        return run

    def test_one_press_starts_a_running_copy_ready_article(self):
        self.assertFalse(self.runs().exists())
        with self.assertLogs("content_factory.content_first_article", level="INFO") as logs:
            response = self.press({"profile": {"name": "Banksia Physio", "oneLiner": "Booking for physio clinics"},
                                   "client_request_id": "client-chosen"})
        run = self.assertStarted(response)
        # Profile edits saved first.
        self.company.refresh_from_db()
        self.assertEqual(self.company.name, "Banksia Physio")
        self.assertEqual(StartupProfile.objects.get(organization=self.org).short_description,
                         "Booking for physio clinics")
        # The press approved the audience and wrote the IF-08 receipt.
        catalog = self.strategy()["editorial_catalog"]
        [audience] = catalog["audience_options"]
        self.assertEqual(audience["status"], "approved")
        self.assertIn(audience["id"], catalog["approval_receipts"]["audience"])
        # The request id is derived server-side, never from the client.
        request = run.run_request
        self.assertEqual(request["client_request_id"], f"first-article:{self.company.pk}:1")
        self.assertEqual(request["delivery_mode"], "content_only")
        self.assertEqual((request["roo_points_billing_status"], request["roo_points_cost"]), ("entitled", 0))
        self.assertEqual(request["editorial_brief"]["audience_id"], audience["id"])
        self.assertEqual(request["editorial_brief"]["conversion_intent"], "none")
        self.assertEqual([row.idempotency_key.rsplit(":", 2)[-2:] for row in free_rows(self.org)], [["attempt", "1"]])
        self.assertEqual(ContentFactoryDispatchOutbox.objects.count(), 1)
        self.assertEqual(self.debits, [])
        self.admission.assert_called_once()
        self.assertTrue(any(f"founder_first_article_started org={self.org.pk} run={run.run_id} attempt=1 free=True"
                            in line for line in logs.output))
        self.assertEqual(first_article.entitlement_state(self.org, self.owner)["status"], "in_progress")

    def test_replaying_the_same_attempt_starts_nothing(self):
        first = self.assertStarted(self.press())
        replay = self.call(journey_views.FirstArticleStartView, method="post",
                           data={"customTopic": "Something else", "expectedCostPoints": 0})
        self.assertEqual((replay.status_code, replay.data["runId"]), (200, first.run_id))
        self.assertEqual(self.runs().count(), 1)
        self.assertEqual(ContentFactoryDispatchOutbox.objects.count(), 1)
        self.assertEqual(free_rows(self.org).count(), 1)

    def test_an_article_already_in_flight_that_is_not_ours_is_in_progress(self):
        ContentFactoryRun.objects.create(run_id=f"run-{uuid4().hex[:8]}", workflow="article_generation",
                                         domain=self.org.domain, organization=self.org, status="running",
                                         run_request={"client_request_id": "other", "delivery_mode": "content_only"})
        self.assertFalse(self.card()["available"])
        response = self.press()
        self.assertEqual((response.status_code, response.data["reason"]), (409, "in_progress"))

    def test_with_an_on_domain_call_to_action_the_press_approves_the_audience_then_the_offer(self):
        url = f"https://{self.org.domain}/book"
        self.site_profile(primaryCallToAction={"label": "Book a demo", "url": url})
        run = self.assertStarted(self.press())
        catalog = review_payload(self.strategy())
        [audience] = catalog["audience_options"]
        [offer] = catalog["cta_options"]
        self.assertEqual((audience["status"], offer["status"]), ("approved", "approved"))
        self.assertEqual((offer["button_href"], offer["audience_ids"], offer["countries"]), (url, [audience["id"]], ["AU"]))
        brief = run.run_request["editorial_brief"]
        self.assertEqual((brief["conversion_intent"], brief["offer_id"], brief["audience_id"]),
                         ("offer", offer["id"], audience["id"]))

    def test_an_off_domain_or_plain_http_call_to_action_gives_no_offer(self):
        for url in ("https://elsewhere.example/book", f"http://{self.org.domain}/book"):
            with self.subTest(url=url):
                built = self.make_org()
                self.org, self.company, self.built = built.organization, built.company, built
                self.site_profile(primaryCallToAction={"label": "Book a demo", "url": url})
                run = self.assertStarted(self.press(built=built))
                self.assertEqual(run.run_request["editorial_brief"]["conversion_intent"], "none")
                self.assertEqual(review_payload(self.strategy())["cta_options"], [])

    def test_a_refused_offer_mutation_falls_back_and_the_press_still_starts(self):
        self.site_profile(primaryCallToAction={"label": "Book a demo", "url": f"https://{self.org.domain}/book"})
        from . import editorial_views
        real = editorial_views.mutate_catalog_response

        def refuse_offers(*args, **kwargs):
            body = args[1] if len(args) > 1 else kwargs.get("data")
            if any(item.get("kind") == "offer" for item in (body or {}).get("activate_entries") or []):
                return Response({"detail": "Refused."}, status=409)
            return real(*args, **kwargs)

        with patch.object(editorial_views, "mutate_catalog_response", side_effect=refuse_offers):
            run = self.assertStarted(self.press())
        self.assertEqual(run.run_request["editorial_brief"]["conversion_intent"], "none")
        catalog = review_payload(self.strategy())
        self.assertEqual(catalog["cta_options"], [])
        self.assertEqual(catalog["audience_options"][0]["status"], "approved")

    def test_existing_posts_are_named_in_the_brief(self):
        self.site_profile(existingPosts=[{"url": f"https://{self.org.domain}/blog/a", "title": "Physio pricing"}])
        run = self.assertStarted(self.press())
        self.assertIn("Does not repeat these existing posts: Physio pricing",
                      run.run_request["editorial_brief"]["acceptance_criteria"])

    def test_a_custom_topic_starts_too(self):
        run = self.assertStarted(self.press({"topicId": None, "customTopic": "Why physio clinics lose bookings"}))
        self.assertIn("Why physio clinics lose bookings", run.run_request["topic"])

    def test_bad_topic_or_audience_is_a_400_and_starts_nothing(self):
        cases = (({"topicId": "missing"}, "invalid_topic"),
                 ({"topicId": None, "customTopic": "ab"}, "invalid_topic"),
                 ({"customTopic": "Both topic kinds at once"}, "invalid_topic"),
                 ({"audience": {"description": "x"}}, "invalid_audience"),
                 ({"audience": {"id": "not-in-catalogue"}}, "invalid_audience"))
        for data, reason in cases:
            with self.subTest(data=data):
                response = self.press(data)
                self.assertEqual((response.status_code, response.data["reason"]), (400, reason))
                self.assertEqual(set(response.data), {"detail", "reason"})
        self.assertFalse(self.runs().exists())
        self.assertFalse(free_rows(self.org).exists())

    def test_daily_cap_is_a_409(self):
        other = make_new_journey_org()
        first_article._write(organization_id=other.organization.pk, suffix="attempt:1", user=other.owner,
                             reference_id="other")
        with override_settings(ARTICLES_FREE_FIRST_ARTICLE_DAILY_CAP=1):
            response = self.press()
        self.assertEqual((response.status_code, response.data["reason"]), (409, "daily_cap"))
        self.assertFalse(self.runs().exists())

    def test_the_charge_paths_402_and_quote_changed_bodies_pass_through(self):
        quote = Response({"code": "roo_points_quote_changed", "detail": "The price changed.", "costPoints": 12},
                         status=409)
        short = Response({"code": "INSUFFICIENT_ROO_POINTS", "requiredPoints": 12, "currentBalance": 0}, status=402)
        for upstream in (quote, short):
            with self.subTest(status=upstream.status_code), \
                    patch.object(views, "start_article_generation", return_value=upstream):
                response = self.press()
                self.assertEqual((response.status_code, response.data), (upstream.status_code, upstream.data))

    def test_a_non_owner_is_refused(self):
        with patch("community_chat.my_startup_views.user_may_use_organization", return_value=True), \
                patch("founder_tools.services.organization_owner_user_id", return_value=self.owner.pk + 1000):
            response = self.call(journey_views.FirstArticleStartView, method="post",
                                 data={"customTopic": "A topic", "expectedCostPoints": 0})
        self.assertEqual((response.status_code, response.data["reason"]), (403, "owner_only"))
        self.assertFalse(self.runs().exists())


class EmailNormalisationTests(CardFixture, TestCase):
    """One free article per person: plus tags and Gmail dots do not make a new account."""

    def two_accounts(self, first, second):
        a = self.make_org(email=first)
        b = self.make_org(email=second)
        self.assertStarted_for(a)
        return first_article.entitlement_state(b.organization, b.owner)

    def assertStarted_for(self, built):
        response = self.press(built=built)
        self.assertEqual(response.status_code, 201, response.data)

    def test_plus_tags_share_one_entitlement(self):
        self.assertEqual(self.two_accounts(f"jo+a@{uuid4().hex[:6]}.example", "x@x.example")["status"], "available")
        domain = f"{uuid4().hex[:6]}.example"
        self.assertEqual(self.two_accounts(f"jo+a@{domain}", f"jo+b@{domain}")["limit"], "account")

    def test_gmail_dots_and_googlemail_share_one_entitlement(self):
        self.assertEqual(self.two_accounts("j.o@gmail.com", "jo@googlemail.com")["limit"], "account")

    def test_the_normalised_email(self):
        self.assertEqual(first_article.normalised_email(" J.O+news@GoogleMail.com "), "jo@gmail.com")
        self.assertEqual(first_article.normalised_email("j.o+x@example.com"), "j.o@example.com")
        self.assertEqual(first_article.normalised_email("+x@example.com"), "")
        self.assertEqual(first_article.normalised_email("not-an-email"), "")

    def test_with_the_flag_off_todays_exact_match_applies(self):
        domain = f"{uuid4().hex[:6]}.example"
        a = self.make_org(email=f"jo+a@{domain}")
        b = self.make_org(email=f"jo+b@{domain}")
        self.assertStarted_for(a)
        with override_settings(MY_STARTUP_NO_WEBSITE_ARTICLES_ENABLED=False):
            self.assertEqual(first_article.entitlement_state(b.organization, b.owner)["status"], "available")
        self.assertEqual(first_article.entitlement_state(b.organization, b.owner)["limit"], "account")

    def test_a_legacy_org_keeps_todays_exact_match(self):
        domain = f"{uuid4().hex[:6]}.example"
        a = self.make_org(email=f"jo+a@{domain}")
        self.assertStarted_for(a)
        legacy = make_named_legacy_org("mlai", domain=f"{uuid4().hex[:6]}-mlai.au", owner_email=f"jo+b@{domain}")
        now = timezone.now()
        self.assertEqual(first_article._account_rows(legacy.owner, now=now, organization=legacy.organization), [])
        # The same account on a new-journey startup matches the normalised email.
        self.assertEqual(len(first_article._account_rows(legacy.owner, now=now, organization=a.organization)), 1)


class SwitchTopicTests(CardFixture, TestCase):
    def slow_free_run(self):
        response = self.press()
        self.assertEqual(response.status_code, 201, response.data)
        run = ContentFactoryRun.objects.get(run_id=response.data["runId"])
        ContentFactoryRun.objects.filter(pk=run.pk).update(
            status="running", created_at=run.created_at - timedelta(minutes=40))
        run.refresh_from_db()
        return run

    def remote_stop(self, run_id, action, payload, workflow):
        run = ContentFactoryRun.objects.get(run_id=run_id)
        self.stops.append((run_id, action, payload))
        from .website_failure_contract import canonical_bytes
        receipt = {"version": 1, "purpose": "article_cancellation", "run_id": run.run_id, "goal_id": None,
                   "scope": {"github_repo": None, "client_request_id": run.run_request["client_request_id"]},
                   "generation": 0, "state_version": 5, "outcome": "no_article_effect", "cancelled": True,
                   "delivered_value": False, "watermark": {"operation_id": None, "attempt": None, "cancelled": True}}
        receipt["receipt_id"] = sha256(canonical_bytes(receipt)).hexdigest()
        return {"status": "cancelled", "article_cancellation_v1": receipt}

    def switch(self, run, data=None):
        body = {"runId": run.run_id, "customTopic": "A different angle on clinic bookings"}
        body.update(data or {})
        return self.call(journey_views.FirstArticleSwitchTopicView, method="post", data=body)

    def setUp(self):
        super().setUp()
        self.stops = []
        self.enterContext(patch.object(views, "_call_content_factory_run_action", side_effect=self.remote_stop))

    def test_not_available_while_the_run_is_not_slow(self):
        response = self.press()
        run = ContentFactoryRun.objects.get(run_id=response.data["runId"])
        ContentFactoryRun.objects.filter(pk=run.pk).update(status="running")
        refused = self.switch(run)
        self.assertEqual((refused.status_code, refused.data),
                         (409, {"detail": "You can't switch topics right now.", "reason": "not_available"}))
        self.assertEqual(self.stops, [])

    def test_a_slow_free_run_is_superseded_and_attempt_two_starts_free(self):
        run = self.slow_free_run()
        journey = journeys.project_founder_journey(self.org, user=self.owner, company=self.company)
        self.assertTrue(journey["switchTopicAvailable"])
        response = self.switch(run)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.stops[0][1], "cancel")
        self.assertEqual((self.stops[0][2]["cancellation_reason"], self.stops[0][2]["cancelled_by"]),
                         ("superseded", "system"))
        run.refresh_from_db()
        self.assertEqual((run.status, run.result["cancellation_reason"], run.result["cancelled_by"]),
                         ("cancelled", "superseded", "system"))
        second = ContentFactoryRun.objects.get(run_id=response.data["runId"])
        self.assertEqual(second.run_request["client_request_id"], f"first-article:{self.company.pk}:2")
        self.assertEqual(second.run_request["roo_points_billing_status"], "entitled")
        self.assertEqual(self.debits, [])
        # Not an incident: no run_terminal founder alert for the superseded run.
        self.assertNotIn("run_terminal", alerts.run_alert_reasons(run, now=timezone.now()))
        # The journey skips the superseded run and follows attempt 2.
        journey = journeys.project_founder_journey(self.org, user=self.owner, company=self.company)
        self.assertEqual(journey["runId"], second.run_id)

    def test_a_third_attempt_is_refused(self):
        run = self.slow_free_run()
        response = self.switch(run)
        self.assertEqual(response.status_code, 201, response.data)
        second = ContentFactoryRun.objects.get(run_id=response.data["runId"])
        ContentFactoryRun.objects.filter(pk=second.pk).update(
            status="running", created_at=second.created_at - timedelta(minutes=40))
        second.refresh_from_db()
        journey = journeys.project_founder_journey(self.org, user=self.owner, company=self.company)
        self.assertFalse(journey["switchTopicAvailable"])
        refused = self.switch(second)
        self.assertEqual((refused.status_code, refused.data["reason"]), (409, "not_available"))
        self.assertEqual(len(self.stops), 1)

    def test_a_superseded_stop_outside_the_card_still_alerts(self):
        run = ContentFactoryRun.objects.create(
            run_id=f"run-{uuid4().hex[:8]}", workflow="article_generation", domain=self.org.domain,
            organization=self.org, status="cancelled", run_request={"client_request_id": "website-reconcile"},
            result={"cancellation_reason": "superseded", "cancelled_by": "system"})
        self.assertIn("run_terminal", alerts.run_alert_reasons(run, now=run.created_at))
        with override_settings(ARTICLES_FIRST_ARTICLE_CARD_ENABLED=False):
            run.run_request = {"client_request_id": f"first-article:{self.company.pk}:1"}
            self.assertIn("run_terminal", alerts.run_alert_reasons(run, now=run.created_at))


class LegacyAndFlagTests(CardFixture, TestCase):
    """G1 and G4: every first-article endpoint is 404 for a legacy org and with its flag off."""

    def endpoints(self, built):
        return [self.call(journey_views.FirstArticleCardView, built=built),
                self.call(journey_views.FirstArticleStartView, method="post", built=built,
                          data={"customTopic": "A topic", "expectedCostPoints": 0}),
                self.call(journey_views.FirstArticleSwitchTopicView, method="post", built=built,
                          data={"runId": "run-x", "customTopic": "A topic"})]

    def test_the_five_named_legacy_orgs_get_404_with_every_flag_on(self):
        for name in NAMED_LEGACY_ORGS:
            with self.subTest(name=name):
                built = make_named_legacy_org(name)
                before = ContentFactoryRun.objects.filter(organization=built.organization).count()
                with patch.object(views, "start_article_generation") as start:
                    statuses = [response.status_code for response in self.endpoints(built)]
                self.assertEqual(statuses, [404, 404, 404])
                start.assert_not_called()
                self.assertEqual(ContentFactoryRun.objects.filter(organization=built.organization).count(), before)

    def test_flag_off_is_404_for_a_new_journey_org(self):
        with override_settings(ARTICLES_FIRST_ARTICLE_CARD_ENABLED=False):
            statuses = [response.status_code for response in self.endpoints(self.built)]
        self.assertEqual(statuses, [404, 404, 404])
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
            statuses = [response.status_code for response in self.endpoints(self.built)]
        self.assertEqual(statuses, [404, 404, 404])
        self.assertFalse(self.runs().exists())

    def test_routes_resolve_with_and_without_a_slash(self):
        from django.urls import resolve
        for path, view in (("first-article", journey_views.FirstArticleCardView),
                           ("first-article/start", journey_views.FirstArticleStartView),
                           ("first-article/switch-topic", journey_views.FirstArticleSwitchTopicView),
                           ("founder-journey/opt-in", journey_views.FounderJourneyOptInView)):
            for suffix in ("", "/"):
                self.assertIs(resolve(f"/api/v1/my-startup/{path}{suffix}").func.view_class, view)

    def test_the_card_body_carries_no_registry_key(self):
        scanned = {"code", "error_code", "errorCode", "blocking_code", "reasonCode", "reason_code"}

        def keys(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    yield key
                    yield from keys(item)
            elif isinstance(value, list):
                for item in value:
                    yield from keys(item)
        self.assertFalse(scanned & set(keys(self.card())))

    def test_new_strings_have_no_dashes(self):
        for name in dir(cards):
            if name.startswith(("DETAIL_", "FREE_LABEL", "PAID_LABEL")):
                text = getattr(cards, name)
                self.assertNotIn("—", text)
                self.assertNotIn("–", text)
