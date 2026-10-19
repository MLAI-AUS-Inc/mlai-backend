"""Article quotes and debits without a database or network."""
from datetime import datetime, timezone
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace as Obj
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from content_factory.activation import article_capabilities
from content_factory import vibe_marketing_views as views
from content_factory.billing import (
    build_roo_points_authorization_payload,
    build_roo_points_payload,
    get_content_factory_article_cost_points,
    get_content_factory_content_island_topic_cost_points,
)
from integrations.services import article_generation as service
from startup_updates.benefits import has_monthly_update_discount


class ArticleQuoteReadTests(SimpleTestCase):
    def setUp(self):
        self.user = Obj(pk=7, id=7, slack_id="U7", is_active=True)
        self.organization = Obj(pk=9, domain="example.test")
        company = Obj(organization=self.organization)
        profile = Mock()
        profile.companies.select_related.return_value.filter.side_effect = lambda **kw: Obj(
            first=lambda: company if kw["organization__domain__iexact"] == self.organization.domain else None)
        self.profiles = self.enterContext(patch("founder_tools.models.VibeRaisingProfile.objects.filter"))
        self.profiles.return_value.first.return_value = profile
        self.actor = self.enterContext(patch("integrations.services.github_installations.resolve_user_for_actor_id", return_value=self.user))
        self.benefit = self.enterContext(patch("startup_updates.benefits.has_monthly_update_discount", return_value=False))
        self.enterContext(patch("content_factory.company_billing.resolve_content_billing_user", return_value=self.user))
        self.jobs = self.enterContext(patch("content_factory.models.ContentFactoryJob.objects.filter"))
        self.jobs.return_value.first.return_value = None
        self.jobs.return_value.order_by.return_value.first.return_value = None
        self.ledgers = self.enterContext(patch("roo.models.Ledger.objects.filter"))
        self.ledgers.return_value.first.return_value = None
        self.ledgers.return_value.select_related.return_value = self.ledgers.return_value
        self.ledgers.return_value.exists.return_value = False
        self.writes = [self.enterContext(patch(target)) for target in (
            "roo.services.PointsService.spend", "roo.services.PointsService.get_or_create_account",
            "integrations.services.article_generation.require_article_activation",
            "integrations.services.article_generation._ensure_content_factory_user",
            "integrations.services.article_generation.trigger_article_generation",
            "integrations.services.article_generation.http_requests.post")]
        self.addCleanup(lambda: [seam.assert_not_called() for seam in self.writes])

    def quote(self, **fields):
        return service.quote_content_factory_article(actor_id="U7", domain="https://www.example.test/path", **fields)

    def paid_job(self, *, domain="example.test", payer_id=7, research=False):
        prefix = "content_factory:topic_generation:charge:" if research else "content_factory:charge:"
        ledger = Obj(pk=10, id=10, user_id=payer_id, kind="SPEND", source="CONTENT_FACTORY",
            idempotency_key=prefix + "original", reference_type="CONTENT_FACTORY", reference_id="original", delta_microroo=-6_000_000)
        self.ledgers.return_value.first.return_value = ledger
        job = Obj(domain=domain, client_request_id="original", billing_ledger_id=10, billing_status="charged",
            request_meta={"roo_points_action": "content_island_topic_generation" if research else "article_generation"})
        self.jobs.return_value.first.return_value = job
        return job

    def test_new_quote_uses_requester_benefit_without_writes_or_activation(self):
        self.assertEqual(self.quote(), 12)
        self.benefit.return_value = True
        self.assertEqual(self.quote(), 6)
        self.benefit.assert_called_with(self.user)
        self.profiles.assert_called_with(user=self.user, role="founder")

    def test_unowned_domain_and_unmapped_or_inactive_actor_are_rejected(self):
        with self.assertRaises(PermissionError):
            service.quote_content_factory_article(actor_id="U7", domain="other.test")
        for user in (None, Obj(pk=8, is_active=False)):
            self.actor.return_value = user
            with self.subTest(user=user), self.assertRaises(PermissionError):
                self.quote()

    def test_actor_must_match_resolved_user(self):
        self.actor.return_value = Obj(pk=8, slack_id="U8", is_active=True)
        with self.assertRaises(service.ArticleGenerationError):
            self.quote()

    def test_original_six_point_quote_is_scoped_to_same_company_and_payer(self):
        self.paid_job()
        self.assertEqual(self.quote(source_run_id="source"), 6)
        self.paid_job(domain="other.test")
        with self.assertRaises(PermissionError):
            self.quote(source_run_id="source")
        self.paid_job(payer_id=8)
        with self.assertRaises(service.ArticleGenerationError):
            self.quote(source_run_id="source")

    def test_source_only_quote_resolves_saved_domain_then_requires_ownership(self):
        self.paid_job()
        self.assertEqual(service.quote_content_factory_article(actor_id="U7", source_run_id="source"), 6)
        self.profiles.return_value.first.return_value = None
        with self.assertRaises(PermissionError):
            service.quote_content_factory_article(actor_id="U7", source_run_id="source")

    def test_existing_source_shortcut_requires_real_same_payer_article_spend(self):
        job = self.paid_job()
        self.jobs.return_value.order_by.return_value.first.return_value = job
        self.assertIs(service._get_existing_billed_source_job("original", article_request={}, user=self.user, domain="example.test"), job)
        self.ledgers.return_value.first.return_value = None
        with self.assertRaises(service.ArticleGenerationError) as error:
            service._get_existing_billed_source_job("original", article_request={}, user=self.user, domain="example.test")
        self.assertEqual(error.exception.payload["code"], "roo_points_quote_changed")

    def test_paid_source_confirmation_verifies_original_payment_without_another_charge(self):
        job = self.paid_job()
        job.request_meta["requested_by_slack_user_id"] = "U7"
        with patch.object(service, "_charge_content_factory_request") as charge:
            self.assertIs(service._charge_deferred_discovery_job_if_needed(source_job=job, slack_user_id="U7",
                domain="example.test", confirmed_keyword="Founder tools"), job)
            self.assertFalse(job._article_confirmation_charge_created)
        charge.assert_not_called()

    def test_research_payment_does_not_supply_an_article_price(self):
        self.paid_job(research=True)
        self.assertEqual(self.quote(source_run_id="research"), 12)
        self.assertEqual(self.quote(source_run_id="research", client_request_id="content-factory-confirm-research"), 12)

    def test_legacy_confirmation_child_uses_verified_original_six_point_source(self):
        source = self.paid_job()
        source.job_id = "source"
        child = Obj(domain="example.test", request_meta={"source_run_id": "source"})
        original = service._require_confirm_child_payment(child, source_job=source,
            user=self.user, domain="example.test")
        self.assertEqual(original.delta_microroo, -6_000_000)
        for changed in ({"domain": "other.test"}, {"request_meta": {"source_run_id": "other"}},
                {"billing_status": "refunded"}, {"client_request_id": "fabricated"}):
            with self.subTest(changed=changed), self.assertRaises(service.ArticleGenerationError):
                service._require_confirm_child_payment(Obj(**{**vars(child), **changed}),
                    source_job=source, user=self.user, domain="example.test")
        self.paid_job(payer_id=8)
        with self.assertRaises(service.ArticleGenerationError):
            service._require_confirm_child_payment(child, source_job=source,
                user=self.user, domain="example.test")

    def test_confirmation_child_cannot_hide_a_conflicting_payment_or_adopt_a_new_charge(self):
        source = self.paid_job()
        source.job_id = "source"
        source.billing_source_job_id = "source"
        child = Obj(domain="example.test", request_meta={"source_run_id": "source"},
            billing_source_job_id="source", client_request_id="other", billing_ledger_id=11)
        original = self.ledgers.return_value.first.return_value
        other = Obj(**{**vars(original), "pk": 11, "id": 11})
        with patch.object(service, "_require_article_job_payment", side_effect=[original, other]), self.assertRaises(service.ArticleGenerationError):
            service._require_confirm_child_payment(child, source_job=source, user=self.user, domain="example.test")
        source._article_confirmation_charge_created = True
        with self.assertRaises(service.ArticleGenerationError):
            service._require_confirm_child_payment(Obj(domain="example.test", request_meta={"source_run_id": "source"}),
                source_job=source, user=self.user, domain="example.test")

    def test_status_without_payment_proof_cannot_supply_original_quote(self):
        self.paid_job()
        self.ledgers.return_value.first.return_value = None
        with self.assertRaises(service.ArticleGenerationError) as error:
            self.quote(source_run_id="unpaid")
        self.assertEqual(error.exception.payload["code"], "roo_points_quote_changed")

    def test_refunded_original_payment_cannot_supply_a_replay_quote(self):
        self.paid_job()
        self.ledgers.return_value.exists.return_value = True
        with self.assertRaises(service.ArticleGenerationError):
            self.quote(source_run_id="refunded")

    def test_client_request_quote_requires_original_company_lineage(self):
        self.paid_job()
        with patch("content_factory.billing_lineage._same_company_charge", return_value=True) as lineage:
            self.assertEqual(self.quote(client_request_id="original"), 6)
            self.assertEqual(lineage.call_args.kwargs, {"organization_id": 9, "domain": "example.test", "key": "original"})
            lineage.return_value = False
            with self.assertRaises(PermissionError):
                self.quote(client_request_id="original")

    def test_unavailable_company_payer_returns_typed_quote_and_source_denials(self):
        from .content_views import ContentQuoteView
        from .website_contract import WebsiteAuthorityError
        job = self.paid_job()
        with patch("content_factory.company_billing.resolve_content_billing_user", side_effect=WebsiteAuthorityError(
                "company_billing_founder_unavailable", "Update company billing.")):
            response = ContentQuoteView().post(Obj(data={"domain": "example.test", "slack_user_id": "U7"}))
            self.assertEqual((response.status_code, response.data["code"]), (409, "company_billing_founder_unavailable"))
            with self.assertRaises(service.ArticleGenerationError) as error:
                service._require_article_job_payment(job, user=self.user, domain="example.test")
            self.assertEqual(error.exception.payload["code"], "company_billing_founder_unavailable")

    @override_settings(ROO_API_KEY="synthetic-quote-key")
    def test_registered_endpoint_requires_strict_key_and_returns_integer_quote(self):
        from django.urls import resolve
        from .content_views import ContentQuoteView
        factory = APIRequestFactory()
        self.assertIs(resolve("/api/v1/content/quote").func.view_class, ContentQuoteView)
        for headers, expected in (({}, 403), ({"HTTP_X_API_KEY": "synthetic-quote-key"}, 200)):
            request = factory.post("/api/v1/content/quote", {"domain": "example.test", "requested_by_slack_user_id": "U7",
                "billing_user_id": 99}, format="json", **headers)
            response = ContentQuoteView.as_view()(request)
            self.assertEqual(response.status_code, expected)
            if expected == 200:
                self.assertEqual(response.data, {"costPoints": 12})
                self.assertIs(type(response.data["costPoints"]), int)


class ArticleConsentTransportTests(SimpleTestCase):
    def setUp(self):
        self.data = {"domain": "example.test", "slack_user_id": "U7", "request_source": "roo_slackbot",
            "client_request_id": "article-7", "topic": "Founder tools", "confirmed_keyword": "founder tools", "keyword": "founder tools"}
        self.error = service.ArticleGenerationError("Review the updated quote")
        self.error.payload = {"code": "roo_points_quote_changed", "costPoints": 12}

    def test_api_key_generate_and_confirmation_forward_actual_quotes_and_return_409(self):
        from . import content_views as endpoints
        job = Obj(domain="example.test", selected_keyword="founder tools", selection_data={}, request_meta={},
            billing_status="deferred", slack_channel_id="", slack_thread_ts="", slack_root_message_ts="", progress_message_ts="", save=Mock())
        for alias in ("expectedCostPoints", "expected_cost_points"):
            request = Obj(data={**self.data, alias: 6})
            with patch.object(endpoints, "trigger_article_generation", side_effect=self.error) as generate:
                response = endpoints.ContentGenerateView().post(request)
            self.assertEqual((response.status_code, response.data), (409, self.error.payload))
            self.assertEqual(generate.call_args.args[1][alias], 6)
            with patch.object(endpoints, "confirm_topic", side_effect=self.error) as confirm:
                response = endpoints.ContentConfirmView().post(request)
            self.assertEqual((response.status_code, response.data), (409, self.error.payload))
            self.assertEqual(confirm.call_args.kwargs["expected_cost_points"], 6)
            self.assertEqual(confirm.call_args.kwargs["client_request_id"], self.data["client_request_id"])
            with patch("content_factory.models.ContentFactoryJob.objects.get", return_value=job), \
                 patch.object(service, "confirm_topic", side_effect=self.error) as confirm:
                response = endpoints.ContentJobConfirmView().post(request, "source")
            self.assertNotIn("request_meta", job.save.call_args.kwargs["update_fields"])
            self.assertEqual((response.status_code, response.data), (409, self.error.payload))
            self.assertEqual(confirm.call_args.kwargs["expected_cost_points"], 6)
            self.assertEqual(confirm.call_args.kwargs["client_request_id"], self.data["client_request_id"])

    def test_authenticated_app_forwards_actual_quote_for_direct_and_source_articles(self):
        from integrations import api_views_content_factory_app as app
        user = Obj(pk=7, email="founder@example.test", first_name="Test", last_name="Founder", avatar_url="")
        for alias in ("expectedCostPoints", "expected_cost_points"):
            for source in ("", "research"):
                with patch.object(app, "_ensure_actor_id", return_value="mlai_user:7"), \
                     patch.object(app, "_assert_domain_access", return_value=(None, "example.test")), \
                     patch.object(app, "confirm_topic", side_effect=self.error) as confirm, \
                     patch.object(app, "trigger_article_generation", side_effect=self.error) as generate:
                    request = Obj(user=user, data={**self.data, "target_keyword": "founder tools", alias: 6, "source_run_id": source})
                    response = app.ContentFactoryAppArticleView().post(request)
                self.assertEqual((response.status_code, response.data), (409, self.error.payload))
                if source:
                    self.assertEqual(confirm.call_args.kwargs["expected_cost_points"], 6)
                else:
                    self.assertEqual(generate.call_args.args[1][alias], 6)


class ArticleAdmissionQuoteTests(SimpleTestCase):
    def setUp(self):
        self.user = Obj(pk=7, email="requester@example.test")
        self.payer = Obj(pk=8)
        self.context = Obj(organization=Obj(pk=9, id=9, domain="example.test"))
        self.enterContext(patch.object(views, "get_content_factory_article_cost_points", return_value=12))
        self.enterContext(patch("content_factory.company_billing.resolve_content_billing_user", return_value=self.payer))
        self.ledgers = self.enterContext(patch("roo.models.Ledger.objects.filter"))
        self.ledgers.return_value.first.return_value = None
        self.ledgers.return_value.exists.return_value = False
        self.lineage_jobs = self.enterContext(patch("content_factory.models.ContentFactoryJob.objects.filter"))
        self.lineage_jobs.return_value.exists.return_value = False
        outboxes = self.enterContext(patch("content_factory.dispatch_models.ContentFactoryDispatchOutbox.objects.filter"))
        outboxes.return_value.first.return_value = None
        self.editorial = self.enterContext(patch.object(views, "_refresh_article_editorial_payload", return_value=None))
        self.reservation = self.enterContext(patch.object(views, "_reserve_article_start_connection", return_value=None))
        self.admission = self.enterContext(patch.object(views, "_validate_article_start_admission"))
        self.enterContext(patch.object(views, "_validate_article_start_reservation"))
        self.enterContext(patch.object(views.transaction, "atomic", side_effect=nullcontext))
        self.enterContext(patch.object(views, "founder_actor_id_for_user", return_value="founder:7"))
        self.enterContext(patch.object(views, "_roo_points_balance_for_user", return_value=20))
        self.charge = self.enterContext(patch.object(views, "charge_content_factory_request_for_user",
            return_value=(self.payer, Obj(id=10), 12)))
        self.outbox = self.enterContext(patch("content_factory.dispatch_outbox.reserve_dispatch"))

    def charge_request(self, data, **payload):
        return views._charge_roo_points_for_article(Obj(user=self.user, data=data),
            context=self.context, payload=payload)

    def assert_rejected_before_new_work(self, result, *, cost_points=12):
        self.assertEqual(result[:3], (None, None, None))
        self.assertEqual(result[3].status_code, 409)
        self.assertEqual(result[3].data["code"], "roo_points_quote_changed")
        self.assertEqual(result[3].data["costPoints"], cost_points)
        for seam in (self.editorial, self.reservation, self.admission, self.charge, self.outbox):
            seam.assert_not_called()

    def test_new_article_requires_request_body_quote_even_when_payload_has_price(self):
        for data in ({}, {"expectedCostPoints": None}, {"expected_cost_points": None}):
            with self.subTest(data=data):
                self.assert_rejected_before_new_work(self.charge_request(data, expected_cost_points=12))
        self.ledgers.assert_not_called()

    def test_invalid_quotes_cannot_admit_new_articles(self):
        for name in ("expectedCostPoints", "expected_cost_points"):
            for value in (True, False, "12", 12.0, -12, 0, 6, [], {}):
                with self.subTest(name=name, value=value):
                    self.assert_rejected_before_new_work(self.charge_request({name: value}))

    def test_fabricated_or_uncharged_request_id_cannot_replace_consent(self):
        for data, payload in (({"clientRequestId": "uncharged"}, {}),
                ({}, {"client_request_id": "fabricated", "expected_cost_points": 12})):
            with self.subTest(data=data, payload=payload):
                self.assert_rejected_before_new_work(self.charge_request(data, **payload))
        self.assertEqual(self.ledgers.call_args.kwargs,
            {"idempotency_key": "content_factory:charge:fabricated", "kind": "SPEND"})

    def test_foreign_payer_or_non_article_debit_cannot_replace_consent(self):
        for payer_id, source in ((self.user.pk, "CONTENT_FACTORY"), (self.payer.pk, "OTHER")):
            self.ledgers.return_value.first.return_value = Obj(pk=10, user_id=payer_id, kind="SPEND",
                source=source, delta_microroo=-6_000_000, idempotency_key="content_factory:charge:article-7",
                reference_type="CONTENT_FACTORY", reference_id="article-7")
            with self.subTest(payer_id=payer_id, source=source):
                self.assert_rejected_before_new_work(self.charge_request({}, client_request_id="article-7"))

    def test_omitted_quote_retry_accepts_verified_same_payer_original_price(self):
        original = Obj(pk=10, id=10, user_id=self.payer.pk, kind="SPEND", source="CONTENT_FACTORY", delta_microroo=-6_000_000,
            idempotency_key="content_factory:charge:article-7", reference_type="CONTENT_FACTORY", reference_id="article-7")
        self.ledgers.return_value.first.return_value = original
        self.lineage_jobs.return_value.exists.return_value = True
        self.charge.return_value = (self.payer, original, 6)
        result = self.charge_request({}, client_request_id="article-7")
        self.assertEqual(result[:2], (self.payer, original))
        self.assertIsNone(result[3])
        self.assertIsNone(self.charge.call_args.kwargs["article_request"]["expected_cost_points"])
        self.assertEqual(self.outbox.call_args.kwargs["payload"]["roo_points_cost"], 6)

    def test_paid_retry_cannot_bypass_an_invalid_supplied_quote(self):
        self.ledgers.return_value.first.return_value = Obj(pk=10, user_id=self.payer.pk, kind="SPEND",
            source="CONTENT_FACTORY", delta_microroo=-6_000_000, idempotency_key="content_factory:charge:article-7",
            reference_type="CONTENT_FACTORY", reference_id="article-7")
        self.lineage_jobs.return_value.exists.return_value = True
        self.assert_rejected_before_new_work(self.charge_request({"expectedCostPoints": "6"}, client_request_id="article-7"), cost_points=6)

    def test_body_forged_goal_fields_cannot_replace_consent(self):
        fields = {"goal_operation_id": "prepare", "first_article_goal_operation_id": "prepare"}
        with patch("content_factory.website_first_article_billing.generation_key") as internal_key:
            self.assert_rejected_before_new_work(self.charge_request(fields, **fields))
        internal_key.assert_not_called()

    def test_current_confirmed_quote_is_accepted_in_either_spelling(self):
        for name in ("expectedCostPoints", "expected_cost_points"):
            with self.subTest(name=name):
                self.assertIsNone(self.charge_request({name: 12})[3])
        self.ledgers.assert_not_called()

    def test_research_keeps_omitted_quote_compatibility(self):
        self.assertIsNone(views._quoted_price_response(Obj(data={}), cost_points=3))


class ArticleOriginalAdmissionProofTests(SimpleTestCase):
    def test_original_debit_requires_same_company_and_no_refund_before_shared_spend(self):
        user = Obj(pk=7)
        ledger = Obj(pk=10, user_id=7, kind="SPEND", source="CONTENT_FACTORY", delta_microroo=-6_000_000,
            idempotency_key="content_factory:charge:original", reference_type="CONTENT_FACTORY", reference_id="original")
        row = Obj(organization_id=9, payload={"billing_user_id": "7", "request": {
            "domain": "example.test", "roo_points_ledger_id": "10"}})
        for shape in ("same-company", "other-company", "unbound", "refunded"):
            with self.subTest(shape=shape), patch.object(service, "require_article_activation"), \
                 patch("content_factory.company_billing.resolve_content_billing_user", return_value=user), \
                 patch("roo.models.Ledger.objects.filter") as ledgers, \
                 patch("organizations.models.Organization.objects.filter") as organizations, \
                 patch("content_factory.models.ContentFactoryJob.objects.filter") as jobs, \
                 patch("content_factory.dispatch_models.ContentFactoryDispatchOutbox.objects.filter") as outboxes, \
                 patch("roo.services.PointsService.spend", return_value=(ledger, False)) as spend:
                ledgers.return_value.first.return_value = ledger
                ledgers.return_value.exists.return_value = shape == "refunded"
                organizations.return_value.only.return_value.first.return_value = Obj(pk=20 if shape == "other-company" else 9)
                jobs.return_value.exists.return_value = False
                jobs.return_value.order_by.return_value.first.return_value = None
                outboxes.return_value.first.return_value = None if shape == "unbound" else row
                kwargs = dict(user=user, created_by_slack_id="mlai_user:7", article_request={"client_request_id": "original"},
                    resolved_domain="other.test" if shape == "other-company" else "example.test")
                if shape == "same-company":
                    self.assertEqual(service._charge_content_factory_user(**kwargs)[2], 6)
                    spend.assert_called_once()
                else:
                    with self.assertRaises(service.ArticleGenerationError) as error:
                        service._charge_content_factory_user(**kwargs)
                    self.assertEqual(error.exception.code, "content_factory_billing_key_conflict")
                    spend.assert_not_called()


    def test_concurrent_reused_debit_is_revalidated_for_company_and_refund(self):
        user = Obj(pk=7)
        ledger = Obj(pk=10, user_id=7, kind="SPEND", source="CONTENT_FACTORY", delta_microroo=-12_000_000,
            idempotency_key="content_factory:charge:original", reference_type="CONTENT_FACTORY", reference_id="original")
        for shape in ("same-company", "other-company", "refunded"):
            with self.subTest(shape=shape), patch.object(service, "require_article_activation"), \
                 patch.object(service, "get_content_factory_article_cost_points", return_value=12), \
                 patch("content_factory.company_billing.resolve_content_billing_user", return_value=user), \
                 patch("roo.models.Ledger.objects.filter") as ledgers, \
                 patch("organizations.models.Organization.objects.filter") as organizations, \
                 patch("content_factory.models.ContentFactoryJob.objects.filter") as jobs, \
                 patch("content_factory.dispatch_models.ContentFactoryDispatchOutbox.objects.filter") as outboxes, \
                 patch("roo.services.PointsService.spend", return_value=(ledger, False)) as spend:
                ledgers.return_value.first.side_effect = [None, ledger]
                ledgers.return_value.exists.return_value = shape == "refunded"
                organizations.return_value.only.return_value.first.return_value = Obj(pk=9)
                jobs.return_value.order_by.return_value.first.return_value = None
                outboxes.return_value.first.return_value = Obj(organization_id=20 if shape == "other-company" else 9,
                    payload={"billing_user_id": "7", "request": {"domain": "example.test", "roo_points_ledger_id": "10"}})
                kwargs = dict(user=user, created_by_slack_id="mlai_user:7", article_request={
                    "client_request_id": "original", "expected_cost_points": 12}, resolved_domain="example.test")
                if shape == "same-company":
                    self.assertEqual(service._charge_content_factory_user(**kwargs)[2], 12)
                else:
                    with self.assertRaises(service.ArticleGenerationError) as error:
                        service._charge_content_factory_user(**kwargs)
                    self.assertEqual(error.exception.code, "content_factory_billing_key_conflict")
                spend.assert_called_once()


class ArticlePricingTests(SimpleTestCase):
    def setUp(self):
        self.user = Obj(pk=7, email="pricing@example.test")

    def confirmation_io(self, job):
        self.enterContext(patch.object(service, "require_article_activation"))
        self.enterContext(patch.object(service, "_find_active_confirm_child_job", return_value=None))
        self.enterContext(patch.object(service.transaction, "atomic", side_effect=nullcontext))
        rows = self.enterContext(patch("content_factory.models.ContentFactoryJob.objects.select_for_update"))
        rows.return_value.get.return_value = job
        ledgers = self.enterContext(patch("roo.models.Ledger.objects.filter"))
        ledgers.return_value.first.return_value = None
        ledgers.return_value.exists.return_value = False
        return rows, ledgers

    def test_every_domain_has_twelve_point_base_price(self):
        for domain in ("example.com", "mlai.au", "https://www.mlai.au", ""):
            with self.subTest(domain=domain):
                self.assertEqual(get_content_factory_article_cost_points(domain), 12)

    def test_current_monthly_benefit_halves_every_domain_price(self):
        with patch("startup_updates.benefits.has_monthly_update_discount", return_value=True) as benefit:
            for domain in ("example.com", "mlai.au"):
                self.assertEqual(get_content_factory_article_cost_points(domain, user=self.user), 6)
            benefit.assert_called_with(self.user)

    def test_missing_or_expired_benefit_keeps_twelve_point_price(self):
        with patch("startup_updates.benefits.has_monthly_update_discount", return_value=False):
            self.assertEqual(get_content_factory_article_cost_points("mlai.au", user=self.user), 12)

    def test_shared_benefit_uses_existing_eligibility_and_melbourne_day(self):
        now = datetime(2026, 10, 18, 14, 0, tzinfo=timezone.utc)
        with patch("django.utils.timezone.now", return_value=now), \
             patch("roo.services.CoworkingService._has_ready_monthly_update", return_value=True) as eligible:
            self.assertTrue(has_monthly_update_discount(self.user))
        self.assertEqual(eligible.call_args.args, (self.user, datetime(2026, 10, 19).date()))

    def test_capabilities_quote_the_requesting_founders_price(self):
        with patch("startup_updates.benefits.has_monthly_update_discount", return_value=True):
            discounted = article_capabilities(None, domain="example.com", user=self.user)
        regular = article_capabilities(None, domain="example.com")
        self.assertEqual(discounted["prices"]["article"], 6)
        self.assertEqual(regular["prices"]["article"], 12)
        self.assertEqual(discounted["prices"]["research"], regular["prices"]["research"])

    def test_article_payload_has_no_free_domain_exception(self):
        payload = build_roo_points_payload(domain="mlai.au", action="article_generation", current_balance=5)
        self.assertEqual((payload["cost_points"], payload["required_points"], payload["free_domain"]), (12, 12, False))
        for cost in (6, 12):
            auth = build_roo_points_authorization_payload(domain="mlai.au", action="article_generation",
                cost_points=cost, billing_status="charged", ledger_id=9)
            self.assertEqual(auth["roo_points_required"], cost)
            self.assertFalse(auth["free_domain"])
        self.assertEqual(get_content_factory_content_island_topic_cost_points("mlai.au"), 0)

    def charge(self, *, price, expected, existing=None, require_new_charge=False, created=None):
        with patch.object(service, "require_article_activation"), \
             patch("content_factory.company_billing.resolve_content_billing_user", return_value=self.user), \
             patch("roo.models.Ledger.objects.filter") as ledgers, \
             patch("organizations.models.Organization.objects.filter") as organizations, \
             patch("content_factory.dispatch_models.ContentFactoryDispatchOutbox.objects.filter") as outboxes, \
             patch.object(service, "get_content_factory_article_cost_points", return_value=price), \
             patch("content_factory.models.ContentFactoryJob.objects") as jobs, \
             patch("roo.services.PointsService.spend", return_value=(Obj(pk=9), existing is None if created is None else created)) as spend:
            self.last_spend = spend
            ledgers.return_value.first.return_value = (Obj(pk=9, user_id=7, kind="SPEND", source="CONTENT_FACTORY",
                idempotency_key="content_factory:charge:article-7", reference_type="CONTENT_FACTORY", reference_id="article-7",
                delta_microroo=existing.delta * 1_000_000) if existing else None)
            ledgers.return_value.exists.return_value = False
            organizations.return_value.only.return_value.first.return_value = Obj(pk=9)
            outboxes.return_value.first.return_value = None
            jobs.filter.return_value.exists.return_value = bool(existing)
            jobs.filter.return_value.order_by.return_value.first.return_value = None
            result = service._charge_content_factory_user(user=self.user, created_by_slack_id="mlai_user:7",
                article_request={"client_request_id": "article-7", "expectedCostPoints": expected}, resolved_domain="mlai.au",
                require_new_charge=require_new_charge)
        return result, spend

    def test_original_charge_from_a_different_payer_has_a_typed_conflict(self):
        with patch.object(service, "require_article_activation"), \
             patch("content_factory.company_billing.resolve_content_billing_user", return_value=Obj(pk=8)), \
             patch("roo.models.Ledger.objects.filter") as ledger, \
             patch("roo.services.PointsService.spend") as spend:
            ledger.return_value.first.return_value = Obj(user_id=self.user.pk, source="CONTENT_FACTORY")
            with self.assertRaises(service.ArticleGenerationError) as error:
                service._charge_content_factory_user(user=self.user, created_by_slack_id="mlai_user:7",
                    article_request={"client_request_id": "article-7"}, resolved_domain="mlai.au")
        self.assertEqual(error.exception.code, "content_factory_billing_key_conflict")
        spend.assert_not_called()

    def test_regular_and_discounted_quotes_are_the_actual_debit(self):
        for price in (6, 12):
            result, spend = self.charge(price=price, expected=price)
            self.assertEqual(result[2], price)
            self.assertEqual(spend.call_args.kwargs["delta"], price)

    def test_changed_quote_rejects_before_spending(self):
        with self.assertRaises(service.ArticleGenerationError) as error:
            self.charge(price=12, expected=6)
        self.assertEqual(error.exception.payload["costPoints"], 12)
        self.last_spend.assert_not_called()

    def test_new_shared_article_charge_requires_a_valid_confirmed_quote(self):
        for expected in (None, True, "12", 12.0, 0):
            with self.subTest(expected=expected), self.assertRaises(service.ArticleGenerationError) as error:
                self.charge(price=12, expected=expected)
            self.assertEqual(error.exception.payload["code"], "roo_points_quote_changed")
            self.assertEqual(error.exception.payload["costPoints"], 12)
            self.last_spend.assert_not_called()

    def test_shared_original_paid_retry_may_omit_its_quote(self):
        result, spend = self.charge(price=12, expected=None, existing=Obj(delta=-6))
        self.assertEqual(result[2], 6)
        self.assertEqual(spend.call_args.kwargs["delta"], 6)

    def test_new_confirmation_rejects_an_original_or_concurrently_reused_spend(self):
        with self.assertRaises(service.ArticleGenerationError) as original:
            self.charge(price=12, expected=6, existing=Obj(delta=-6), require_new_charge=True)
        self.assertEqual(original.exception.code, "content_factory_billing_key_conflict")
        self.last_spend.assert_not_called()
        with self.assertRaises(service.ArticleGenerationError) as concurrent:
            self.charge(price=12, expected=12, created=False, require_new_charge=True)
        self.assertEqual(concurrent.exception.code, "content_factory_billing_key_conflict")

    def test_retry_retains_original_discount_after_expiry(self):
        result, spend = self.charge(price=12, expected=6, existing=Obj(delta=-6))
        self.assertEqual(result[2], 6)
        self.assertEqual(spend.call_args.kwargs["delta"], 6)

    def test_research_confirmation_passes_its_article_quote_to_the_debit(self):
        for price in (6, 12):
            original = {"roo_points_action": "content_island_topic_generation", "expectedCostPoints": 3,
                "client_request_id": "research-key", "requested_by_slack_user_id": "mlai_user:99"}
            job = Obj(pk=7, job_id="research-7", domain="mlai.au", client_request_id="research-key", request_meta=original,
                billing_status="charged", save=Mock())
            self.confirmation_io(job)
            with patch.object(service, "_charge_content_factory_request", return_value=(self.user, Obj(pk=9), price)) as charge:
                service._charge_deferred_discovery_job_if_needed(source_job=job, slack_user_id="mlai_user:7",
                    domain="mlai.au", confirmed_keyword="Founder tools", expected_cost_points=price,
                    client_request_id="content-factory-confirm-research-7", billing_user=self.user,
                    requested_by_slack_user_id="mlai_user:7")
            self.assertEqual(charge.call_args.args[1]["expected_cost_points"], price)
            self.assertEqual(charge.call_args.args[1]["client_request_id"], "content-factory-confirm-research-7")
            self.assertEqual(job.client_request_id, "content-factory-confirm-research-7")
            self.assertEqual(charge.call_args.args[1]["requested_by_slack_user_id"], "mlai_user:7")
            self.assertEqual(service.require_article_activation.call_args.kwargs["actor_id"], "mlai_user:7")
            self.assertEqual(job.billing_amount, price)
            self.assertEqual(original["expectedCostPoints"], 3)
            self.assertEqual(original["requested_by_slack_user_id"], "mlai_user:99")

    def test_research_or_deferred_confirmation_cannot_invent_article_consent(self):
        for research in (False, True):
            original = {"client_request_id": "research-key", "expectedCostPoints": 3, "expected_cost_points": 3}
            if research:
                original["roo_points_action"] = "content_island_topic_generation"
            job = Obj(pk=7, job_id="research-7", domain="example.test", client_request_id="research-key", request_meta=original,
                billing_status="charged" if research else "deferred", save=Mock())
            self.confirmation_io(job)
            with self.subTest(research=research), patch.object(service, "require_article_activation"), \
                 patch.object(service, "_validate_authenticated_content_factory_actor"), \
                 patch("content_factory.company_billing.resolve_content_billing_user", return_value=self.user), \
                 patch.object(service, "get_content_factory_article_cost_points", return_value=12), \
                 patch("roo.services.PointsService.spend") as spend:
                with self.assertRaises(service.ArticleGenerationError) as error:
                    service._charge_deferred_discovery_job_if_needed(source_job=job, slack_user_id="mlai_user:7",
                        domain="example.test", confirmed_keyword="Founder tools", billing_user=self.user)
                self.assertEqual(error.exception.payload["code"], "roo_points_quote_changed")
                spend.assert_not_called()
                job.save.assert_not_called()
                self.assertEqual(original["expectedCostPoints"], 3)
                self.assertEqual(original["expected_cost_points"], 3)

    def test_source_only_research_or_deferred_confirmation_keeps_saved_stable_key(self):
        for research in (False, True):
            metadata = {"client_request_id": "persisted-source-key"}
            if research:
                metadata["roo_points_action"] = "content_island_topic_generation"
            source = Obj(pk=7, job_id="research-7", domain="example.test", client_request_id="persisted-source-key",
                request_meta=metadata, billing_status="charged" if research else "deferred", save=Mock())
            self.confirmation_io(source)
            with self.subTest(research=research), patch.object(service, "_charge_content_factory_request",
                    return_value=(self.user, Obj(pk=9), 12)) as charge:
                result = service._charge_deferred_discovery_job_if_needed(source_job=source, slack_user_id="mlai_user:7",
                    domain="example.test", confirmed_keyword="Founder tools", expected_cost_points=12, billing_user=self.user)
            self.assertEqual(charge.call_args.args[1]["client_request_id"], "persisted-source-key")
            self.assertEqual(result.client_request_id, "persisted-source-key")
            self.assertEqual(result.request_meta["client_request_id"], "persisted-source-key")
            self.assertEqual(result.billing_amount, 12)
            self.assertTrue(result._article_confirmation_charge_created)
            charge.assert_called_once()

    def test_research_source_cannot_adopt_or_refund_an_unrelated_article_key(self):
        job = Obj(pk=7, domain="example.test", job_id="research", client_request_id="research-key",
            request_meta={"roo_points_action": "content_island_topic_generation"}, billing_status="charged", save=Mock())
        _, ledgers = self.confirmation_io(job)
        for existing in ((True,), (False, True)):
            ledgers.return_value.exists.side_effect = existing
            with self.subTest(existing=existing), patch.object(service, "_charge_content_factory_request") as charge:
                with self.assertRaises(service.ArticleGenerationError) as error:
                    service._charge_deferred_discovery_job_if_needed(source_job=job, slack_user_id="mlai_user:7",
                        domain="example.test", confirmed_keyword="Founder tools", client_request_id="unrelated-paid-key")
                self.assertEqual(error.exception.code, "content_factory_billing_key_conflict")
                charge.assert_not_called()
                job.save.assert_not_called()

    def test_existing_unpaid_child_rejects_confirmation_before_source_debit(self):
        source = Obj(pk=7, domain="example.test", job_id="research", client_request_id="research-key",
            request_meta={"roo_points_action": "content_island_topic_generation"}, billing_status="charged",
            billing_ledger_id=None, billing_amount=3, slack_channel_id="", slack_thread_ts="",
            slack_root_message_ts="", progress_message_ts="", save=Mock())
        child = Obj(domain="example.test", request_meta={"source_run_id": "research"})
        self.confirmation_io(source)
        ledger = Obj(pk=9, user_id=7, idempotency_key="content_factory:charge:article-key", delta_microroo=-12_000_000)
        with patch("content_factory.models.ContentFactoryJob.objects.filter") as jobs, \
             patch.object(service, "_find_active_confirm_child_job", return_value=child), \
             patch.object(service, "_charge_content_factory_request", return_value=(self.user, ledger, 12)) as charge, \
             patch("content_factory.billing_lineage.original_spend_for_job", side_effect=lambda job: ledger if getattr(job, "client_request_id", None) == "article-key" else None), \
             patch("content_factory.company_billing.resolve_content_billing_user", return_value=self.user), \
             patch.object(service, "get_content_factory_article_cost_points", return_value=12):
            jobs.return_value.first.return_value = source
            with self.assertRaises(service.ArticleGenerationError):
                service.confirm_topic(domain="example.test", confirmed_keyword="Founder tools", slack_user_id="mlai_user:7",
                    source_run_id="research", client_request_id="article-key", expected_cost_points=12, billing_user=self.user)
        charge.assert_not_called()
        source.save.assert_not_called()

    def test_competing_key_reloads_paid_source_under_lock_before_another_debit(self):
        stale = Obj(pk=7, domain="example.test", job_id="research", request_meta={"roo_points_action": "content_island_topic_generation"}, billing_status="charged")
        paid = Obj(domain="example.test", client_request_id="first-article-key", request_meta={"roo_points_action": "article_generation"}, billing_status="charged")
        active = {"locked": False}
        @contextmanager
        def atomic():
            active["locked"] = True
            try:
                yield
            finally:
                active["locked"] = False
        def provider(**kwargs):
            self.assertFalse(active["locked"])
        def reload(**kwargs):
            self.assertTrue(active["locked"])
            self.assertEqual(kwargs, {"pk": 7})
            return paid
        with patch.object(service, "require_article_activation", side_effect=provider), \
             patch.object(service.transaction, "atomic", side_effect=atomic), \
             patch("content_factory.models.ContentFactoryJob.objects.select_for_update") as rows, \
             patch.object(service, "_charge_content_factory_request") as charge:
            rows.return_value.get.side_effect = reload
            with self.assertRaises(service.ArticleGenerationError) as error:
                service._charge_deferred_discovery_job_if_needed(source_job=stale, slack_user_id="mlai_user:7",
                    domain="example.test", confirmed_keyword="Founder tools", expected_cost_points=12, client_request_id="second-article-key")
        self.assertEqual(error.exception.code, "content_factory_billing_key_conflict")
        charge.assert_not_called()

    def test_confirmation_without_research_uses_paid_direct_generation(self):
        for request_id in (None, "confirmed-topic-7"):
            with patch.object(service, "require_article_activation"), \
                 patch.object(service, "get_content_factory_article_cost_points", return_value=6), \
                 patch.object(service, "uuid4", return_value=Obj(hex="generated-request")), \
                 patch.object(service, "trigger_article_generation", return_value={"job_id": "paid-article"}) as generate:
                result = service.confirm_topic(domain="mlai.au", confirmed_keyword="founder tools",
                    slack_user_id="mlai_user:7", billing_user=self.user, expected_cost_points=6,
                    client_request_id=request_id, custom_title="Founder tools", skip_alternatives=["other topic"])
            self.assertEqual(result["job_id"], "paid-article")
            payload = generate.call_args.args[1]
            self.assertEqual(payload["expected_cost_points"], 6)
            self.assertEqual(payload["client_request_id"], request_id or "confirm-topic:generated-request")
            self.assertEqual(payload["custom_title"], "Founder tools")
            self.assertEqual(payload["skip_alternatives"], ["other topic"])
            self.assertIs(generate.call_args.kwargs["billing_user"], self.user)

    def test_direct_confirmation_timeout_preserves_the_confirmation_error_contract(self):
        unavailable = service.ContentFactoryBackendUnavailableError({
            "error_code": "CONTENT_FACTORY_UNAVAILABLE", "operation": "queue_article", "retryable": True})
        with patch.object(service, "require_article_activation"), \
             patch.object(service, "get_content_factory_article_cost_points", return_value=12), \
             patch.object(service, "trigger_article_generation", side_effect=unavailable):
            with self.assertRaises(service.ContentFactoryBackendUnavailableError) as error:
                service.confirm_topic(domain="mlai.au", confirmed_keyword="founder tools",
                    slack_user_id="mlai_user:7", billing_user=self.user, client_request_id="confirm-7")
        self.assertEqual(error.exception.payload, {**unavailable.payload, "operation": "confirm_topic"})

    def test_missing_source_run_cannot_create_a_new_paid_article(self):
        with patch.object(service, "require_article_activation"), \
             patch.object(service, "get_content_factory_article_cost_points", return_value=6), \
             patch("content_factory.models.ContentFactoryJob.objects") as jobs, \
             patch.object(service, "trigger_article_generation") as generate:
            jobs.filter.return_value.first.return_value = None
            with self.assertRaises(service.ArticleGenerationError):
                service.confirm_topic(domain="mlai.au", confirmed_keyword="founder tools",
                    slack_user_id="mlai_user:7", billing_user=self.user, source_run_id="missing")
        generate.assert_not_called()
