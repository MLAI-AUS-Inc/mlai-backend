"""A21: the not-on-GitHub exit writes one lead row and one funnel event, behind the gate."""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import resolve
from django.utils import timezone
from rest_framework.test import APIClient

from content_factory.founder_funnel import STAGES, journey_funnel
from core.models import User
from crm.models import Contact

from .website_leads import LEADS_KEY, FounderWebsiteLeadView, normalise_lead_domain

URL = "/api/v1/founder-tools/website-lead/"
GATE = {"ARTICLES_FIRST_ARTICLE_REQUIRES_CONNECTED_WEBSITE": True, "MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED": True}


class WebsiteLeadTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.user = User.objects.create_user(email="founder@lead.example.test", email_verified_at=timezone.now())
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def post(self, body):
        from unittest.mock import patch
        with patch("content_factory.founder_funnel.emit_event") as emit:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(URL, body, format="json")
        events = [call for call in emit.call_args_list if call.args[0] == "not_on_github"]
        for call in events:
            self.assertEqual((call.kwargs["user_id"], call.kwargs["source"]), (self.user.pk, "website_lead.recorded"))
        return response, events

    def leads(self):
        contact = Contact.objects.filter(email=self.user.email).first()
        return (contact.attributes.get(LEADS_KEY) or {}) if contact else {}

    def test_gate_off_records_nothing(self):
        response = self.client.post(URL, {"reason": "not_on_github"}, format="json")
        self.assertEqual((response.status_code, response.data["reason"]), (404, "not_enabled"))
        self.assertFalse(Contact.objects.exists())

    @override_settings(**GATE)
    def test_exit_writes_one_lead_and_one_funnel_event_and_tell_me_updates_it(self):
        response, events = self.post({"reason": "not_on_github", "domain": "https://www.Lovable-Site.example.com/about"})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["recorded"])
        self.assertEqual(response.data["lead"]["domain"], "lovable-site.example.com")
        self.assertFalse(response.data["lead"]["notifyWhenChanged"])
        self.assertIsNone(response.data["lead"]["remindAt"])
        self.assertIn("on GitHub", response.data["message"])
        self.assertEqual(len(events), 1)
        self.assertEqual(len(self.leads()), 1)

        response, events = self.post({"reason": "not_on_github", "domain": "lovable-site.example.com",
                                      "notifyWhenChanged": True})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["lead"]["notifyWhenChanged"])
        self.assertIsNotNone(response.data["lead"]["remindAt"])
        self.assertIn("email you when that changes", response.data["message"])
        self.assertEqual(events, [])
        leads = self.leads()
        self.assertEqual(len(leads), 1)
        lead = next(iter(leads.values()))
        self.assertEqual((lead["userId"], lead["reason"], lead["hostClass"], lead["notifyWhenChanged"]),
                         (self.user.pk, "not_on_github", "unknown", True))
        contact = Contact.objects.get(email=self.user.email)
        self.assertEqual((contact.user_id, contact.source, contact.marketing_consent),
                         (self.user.pk, Contact.Source.CHAT_ACCOUNT, Contact.MarketingConsent.UNKNOWN))

        # A later plain visit keeps the request to be told.
        response, _events = self.post({"reason": "not_on_github", "domain": "lovable-site.example.com"})
        self.assertTrue(response.data["lead"]["notifyWhenChanged"])

    @override_settings(**GATE)
    def test_the_funnel_counts_the_exit_from_the_lead_row(self):
        self.post({"reason": "no_github_account", "hostClass": "ai_builder"})
        now = timezone.now()
        report = journey_funnel(since=now - timedelta(days=1), until=now + timedelta(days=1))
        self.assertIn("not_on_github", STAGES)
        self.assertEqual(report["stages"]["not_on_github"]["users"], 1)
        self.assertEqual(report["stages"]["not_on_github"]["timedUsers"], 1)

    @override_settings(**GATE)
    def test_invalid_reason_and_flag_values_are_refused(self):
        for body in ({"reason": "something_else"}, {"notifyWhenChanged": "yes"}):
            with self.subTest(body=body):
                response = self.client.post(URL, body, format="json")
                self.assertEqual((response.status_code, response.data["reason"]), (400, "invalid_lead"))
        self.assertFalse(Contact.objects.exists())

    @override_settings(**GATE)
    def test_another_accounts_contact_and_an_unverified_email_are_never_written(self):
        other = User.objects.create_user(email="someone@lead.example.test", email_verified_at=timezone.now())
        Contact.objects.create(email=self.user.email, source=Contact.Source.MANUAL, user=other,
                               attributes={"kept": True})
        response, _events = self.post({"reason": "not_in_git"})
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.data["recorded"])
        self.assertEqual(Contact.objects.get(email=self.user.email).attributes, {"kept": True})

        unverified = User.objects.create_user(email="unverified@lead.example.test")
        self.user = unverified
        self.client.force_authenticate(unverified)
        response, _events = self.post({"reason": "not_in_git"})
        self.assertFalse(response.data["recorded"])
        self.assertFalse(Contact.objects.filter(email=unverified.email).exists())

    @override_settings(**GATE)
    def test_an_unstored_exit_still_emits_one_funnel_event(self):
        unverified = User.objects.create_user(email="unverified-repeat@lead.example.test")
        self.user = unverified
        self.client.force_authenticate(unverified)
        first, events = self.post({"reason": "not_on_github"})
        self.assertEqual((first.status_code, first.data["recorded"], len(events)), (201, False, 1))
        again, events = self.post({"reason": "not_on_github", "notifyWhenChanged": True})
        self.assertEqual((again.status_code, len(events)), (200, 0))

    def test_lead_rows_are_system_owned_crm_attributes(self):
        from crm.engagement import RESERVED_ATTRIBUTE_KEYS
        self.assertIn(LEADS_KEY, RESERVED_ATTRIBUTE_KEYS)

    def test_domain_is_normalised_text_only(self):
        self.assertEqual(normalise_lead_domain("HTTPS://www.Example.co/about"), "example.co")
        for value in ("", "localhost", "not a domain", "http://10.0.0.1/", "a" * 300 + ".com"):
            with self.subTest(value=value):
                self.assertEqual(normalise_lead_domain(value), "")

    def test_my_startup_alias_serves_the_view(self):
        match = resolve("/api/v1/my-startup/founder-tools/website-lead/")
        self.assertTrue(issubclass(match.func.view_class, FounderWebsiteLeadView))
        self.assertEqual(match.func.view_class.startup_read_bucket, "read")
