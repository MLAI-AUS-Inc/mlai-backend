"""Initial display offers cannot become native access or a paid setup grant."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import cms_onboarding_offer as offers
from . import website_publishing_channels as channels
from .website_channel_observation import classify_prefix
from .website_contract import WebsiteAuthorityError


@override_settings(ARTICLES_CMS_ONBOARDING_ENABLED=True, ARTICLES_CMS_SETUP_ENABLED=True,
    ARTICLES_CHANNEL_DISCOVERY_ENABLED=True, ARTICLES_PUBLISHING_CHANNELS_ENABLED=True, ARTICLES_CMS_CHANNEL_ENABLED=True,
    ARTICLES_CHANNEL_ARTIFACTS_ENABLED=True, ARTICLES_CMS_GOALS_ENABLED=True, ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True, ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True, ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class CmsOnboardingOfferTests(SimpleTestCase):
    def test_nullable_new_config_repository_is_still_an_initial_site(self):
        self.config.github_repo = None
        self.assertEqual(self.read()["scope"], self.scope)


    def setUp(self):
        self.now = timezone.now()
        self.org = SimpleNamespace(pk=7, domain="native.example.com")
        self.user = SimpleNamespace(pk=1, is_active=True)
        self.profile = SimpleNamespace(pk=3, user_id=1, role="founder")
        self.company = SimpleNamespace(pk=uuid4(), profile_id=3, organization_id=7, domain=self.org.domain)
        self.context = SimpleNamespace(organization=self.org, profile=self.profile, company=self.company)
        self.config = SimpleNamespace(pk=9, organization_id=7, website_connection=None, article_system={})
        for row in (self.org, self.user, self.profile, self.company, self.config): row.refresh_from_db = Mock()
        self.scope = channels.scope_for(self.context, self.config)
        self.config.article_system = {"publishing_channel": {"schema_version": 1, "revision": 2, "scope": deepcopy(self.scope),
            "recommendation": channels.recommend(classify_prefix(b'<head><meta name="generator" content="WordPress"></head>')),
            "selection": {"channel": "cms", "platform": "wordpress"}, "observed_at": self.now.isoformat()}}
        self.data = {"platform": "wordpress", "public_origin": "https://native.example.com/", "api_origin": "https://native.example.com/wordpress/",
            "channel_scope": self.scope, "channel_revision": 2}
        price = patch("content_factory.billing.get_content_factory_article_cost_points", return_value=6)
        self.price = price.start()
        self.addCleanup(price.stop)

    def read(self, **changes):
        return offers.for_context(self.context, self.config, user=self.user, data={**self.data, **changes}, now=self.now)

    def test_actual_initial_owner_offer_has_exact_destinations_scope_price_and_no_work_permission(self):
        before = deepcopy(self.config.article_system)
        with patch("content_factory.cms_owner_connection.inspect_identity") as http, \
                patch("roo.services.PointsService.spend") as debit:
            value = self.read()
        self.assertEqual((value["first_article_points"], value["setup_points"], value["target"]), (6, 0, "first_article_live"))
        self.assertEqual((value["public_origin"], value["api_origin"]), (self.data["public_origin"], self.data["api_origin"]))
        self.assertEqual(value["scope"], self.scope)
        self.assertEqual(offers.validate(value, now=self.now), value)
        self.assertEqual(self.config.article_system, before)
        self.assertIsNone(self.config.website_connection)
        self.assertFalse({"credential", "identity", "operation_id", "binding", "consent", "publishing_ready", "spend_ledger_id"} & set(value))
        http.assert_not_called(); debit.assert_not_called()

    def test_unknown_coerced_or_negative_price_cannot_become_a_free_offer(self):
        for price in (None, True, -1, 1.0, "6", 10**9):
            self.price.return_value = price
            with self.subTest(price=price), self.assertRaises(WebsiteAuthorityError): self.read()
        self.price.return_value = 0
        self.assertEqual(self.read()["first_article_points"], 0)

    def test_changed_price_owner_channel_or_selected_scope_is_refused(self):
        self.price.side_effect = [6, 7]
        with self.assertRaises(WebsiteAuthorityError): self.read()
        self.price.side_effect = None
        for changes in ({"channel_revision": True}, {"channel_revision": 3}, {"channel_scope": {**self.scope, "company_id": str(uuid4())}},
                {"secret": "synthetic-input-not-accepted"}, {"platform": "ghost"}, {"platform": "webflow"}):
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): self.read(**changes)
        self.user.is_active = False
        with self.assertRaises(WebsiteAuthorityError): self.read()

    def test_owner_change_during_price_read_cannot_return_an_old_offer(self):
        def revoked(_domain):
            self.user.is_active = False
            return 6
        self.price.side_effect = revoked
        with self.assertRaises(WebsiteAuthorityError): self.read()

    def test_existing_or_disconnected_original_connections_cannot_be_rebound_as_initial_setup(self):
        for state in ("connected", "paused", "disconnected", "revoked"):
            self.config.website_connection = SimpleNamespace(pk=uuid4(), generation=2, state=state, github_repo="", branch="", app_root="")
            with self.subTest(state=state), self.assertRaises(WebsiteAuthorityError): self.read()

    def test_a_legacy_repository_without_a_connection_is_not_an_initial_cms_site(self):
        self.config.github_repo = "fixture/original-paid-site"
        with self.assertRaises(WebsiteAuthorityError): self.read()
        self.price.assert_not_called()

    def test_public_host_alias_subdirectory_and_ghost_admin_destination_are_explicit(self):
        value = self.read(public_origin="https://www.native.example.com/blog", api_origin="https://www.native.example.com/wordpress")
        self.assertEqual(value["public_origin"], "https://www.native.example.com/blog/")
        self.assertEqual(value["api_origin"], "https://www.native.example.com/wordpress/")
        self.config.article_system["publishing_channel"]["selection"]["platform"] = "ghost"
        self.assertEqual(self.read(platform="ghost", api_origin="https://native.ghost.io/")["api_origin"], "https://native.ghost.io/")

    def test_unconfirmed_foreign_or_credential_bearing_origins_never_enter_an_offer(self):
        for changes in ({"public_origin": "https://foreign.example.com/"}, {"api_origin": "https://foreign.example.com/"},
                {"api_origin": "https://user:password@native.example.com/"}, {"api_origin": "https://native.example.com/?secret=x"},
                {"api_origin": "http://native.example.com/"}, {"public_origin": "https://native.example.com/%2e%2e/"}):
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): self.read(**changes)

    def test_closed_metadata_expires_and_never_accepts_extra_or_coerced_readiness(self):
        value = self.read()
        self.assertEqual(offers.validate(value, now=self.now + timedelta(minutes=14)), value)
        for at in (self.now - timedelta(seconds=1), self.now + offers.TTL):
            with self.assertRaises(WebsiteAuthorityError): offers.validate(value, now=at)
        for changes in ({"version": True}, {"first_article_points": True}, {"publishing_ready": True}, {"digest": "0" * 64},
                {"expires_at": (self.now + timedelta(days=1)).isoformat()}, {"scope": {**self.scope, "owner_user_id": "1"}}):
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): offers.validate({**value, **changes}, now=self.now)

    def test_disabled_initial_rollout_performs_no_owner_refresh_or_quote_read(self):
        with override_settings(ARTICLES_CMS_ONBOARDING_ENABLED=False): self.assertIsNone(self.read())
        self.price.assert_not_called(); self.config.refresh_from_db.assert_not_called()
