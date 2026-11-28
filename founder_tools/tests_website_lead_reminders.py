"""E3b: lead reminders keyed on the lead row, with opt-out, capped and deduplicated."""
from datetime import timedelta
from html import escape
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import User
from crm.models import Contact

from . import website_lead_reminders as reminders
from .website_leads import LEADS_KEY, record_lead

ON = {"MY_STARTUP_WEBSITE_LEAD_REMINDERS_ENABLED": True, "COMMUNITY_CHAT_FRONTEND_URL": "https://chat.example.test",
      "DEFAULT_BACKEND_URL": "https://api.example.test"}


class CapturedEmails:
    """The test email backend: the provider call is replaced, everything before it runs for real."""

    def __init__(self):
        self.outbox = []

    def __call__(self, payload, *, idempotency_key):
        self.outbox.append({"payload": payload, "key": idempotency_key})
        return f"captured-{len(self.outbox)}"

    def __enter__(self):
        self._patch = patch("system_email.transport_resend.send", self)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


def lead(**overrides):
    return {"reason": "not_on_github", "domain": "acme.lovable.app", "hostClass": "ai_builder",
            "guidanceId": "lovable", "notify": True, **overrides}


@override_settings(**ON)
class LeadTemplateTests(SimpleTestCase):
    def test_both_variants_keep_the_setup_and_opt_out_links(self):
        link = "https://chat.example.test/my-startup/start?new=true&step=website"
        opt_out = f"https://api.example.test{reminders.OPT_OUT_PATH}?token=sample"
        for kind in ("reminder", "guidance_changed"):
            with self.subTest(kind=kind):
                branded = reminders._copy(kind, lead(), link, opt_out)
                plain = reminders._copy(kind, lead(), link, opt_out, branded=False)
                self.assertEqual(plain.text, branded.text)
                self.assertEqual(plain.html, "")
                self.assertIn("Continue setup", branded.html)
                self.assertIn(f'href="{escape(link)}"', branded.html)
                self.assertIn(f'href="{opt_out}"', branded.html)

    def test_existing_claim_keeps_plain_text_and_new_claim_has_html(self):
        user = SimpleNamespace(pk=7, email="founder@example.test")
        contact = SimpleNamespace(pk=8, email=user.email)
        old_claim = {"kind": "reminder", "key": "old-key"}
        new_claim = {"kind": "reminder", "key": "new-key", "template": reminders.BRANDED_TEMPLATE}
        send = Mock()
        with patch.object(reminders, "_recipient", return_value=(user, None)), patch.object(reminders, "_close"):
            for claim in (old_claim, new_claim):
                self.assertEqual(reminders._deliver(contact, "lead-key", lead(), claim, send=send), "sent")
        old_delivery, new_delivery = [call.kwargs for call in send.call_args_list]
        self.assertEqual(old_delivery["idempotency_key"], old_claim["key"])
        self.assertEqual(old_delivery["prerendered"].html, "")
        self.assertEqual(new_delivery["idempotency_key"], new_claim["key"])
        self.assertIn(new_delivery["unsubscribe_url"], new_delivery["prerendered"].html)
        self.assertIn(new_delivery["unsubscribe_url"], new_delivery["prerendered"].text)


@override_settings(**ON)
class LeadReminderTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.now = timezone.now()
        self.user = User.objects.create_user(email="founder@reminder.example.test", email_verified_at=self.now)

    def record(self, user=None, at=None, **overrides):
        with patch("content_factory.founder_funnel.emit_event"):
            return record_lead(user or self.user, lead(**overrides), now=at or self.now - timedelta(days=8))

    def leads(self, user=None):
        return Contact.objects.get(email=(user or self.user).email).attributes[LEADS_KEY]

    def sweep(self, at=None, **kwargs):
        with CapturedEmails() as mail:
            counts = reminders.run_lead_reminder_sweep(now=at or self.now, **kwargs)
        return counts, mail.outbox

    def test_a_due_lead_gets_exactly_one_email_with_a_prefilled_deep_link_and_a_second_sweep_sends_nothing(self):
        self.record()
        counts, outbox = self.sweep()
        self.assertEqual(counts["sent"], 1, counts)
        self.assertEqual(len(outbox), 1)
        payload = outbox[0]["payload"]
        self.assertEqual(payload["to"], [self.user.email])
        self.assertIn("GitHub", payload["subject"])
        link = next(word for word in payload["text"].split() if word.startswith("https://chat.example.test/"))
        parts = urlsplit(link)
        self.assertEqual(parts.path, "/my-startup/start")
        self.assertEqual(parse_qs(parts.query), {"new": ["true"], "step": ["website"],
                                                 "website": ["acme.lovable.app"], "builder": ["lovable"]})
        self.assertIn("List-Unsubscribe", payload["headers"])
        self.assertIn(reminders.OPT_OUT_PATH, payload["headers"]["List-Unsubscribe"])
        stored = next(iter(self.leads().values()))
        self.assertIsNone(stored["remindAt"])
        self.assertEqual((stored["reminder"]["kind"], stored["reminder"]["status"]), ("reminder", "sent"))

        counts, outbox = self.sweep(at=self.now + timedelta(days=30))
        self.assertEqual((counts["sent"], outbox), (0, []))
        # A later visit (a redeploy, a refined reason) never re-arms the one reminder.
        self.record(at=self.now + timedelta(days=31), reason="no_github_account")
        self.assertIsNone(next(iter(self.leads().values()))["remindAt"])
        counts, outbox = self.sweep(at=self.now + timedelta(days=60))
        self.assertEqual(outbox, [])

    def test_a_lead_not_yet_due_or_without_tell_me_gets_nothing(self):
        self.record(at=self.now - timedelta(days=2))
        other = User.objects.create_user(email="quiet@reminder.example.test", email_verified_at=self.now)
        self.record(user=other, notify=False)
        counts, outbox = self.sweep()
        self.assertEqual((counts["sent"], outbox), (0, []))

    def test_an_opted_out_lead_gets_none(self):
        self.record()
        contact = Contact.objects.get(email=self.user.email)
        client = APIClient()
        url = reminders.OPT_OUT_PATH
        token = reminders.opt_out_token(contact)
        # Opening the link changes nothing; the button (or one-click POST) does.
        page = client.get(url, {"token": token})
        self.assertEqual(page.status_code, 200)
        self.assertTrue(next(iter(self.leads().values()))["notifyWhenChanged"])
        done = client.post(f"{url}?token={token}", {"List-Unsubscribe": "One-Click"})
        self.assertEqual(done.status_code, 200)
        stored = next(iter(self.leads().values()))
        self.assertEqual((stored["notifyWhenChanged"], stored["remindAt"]), (False, None))
        self.assertTrue(stored["optedOutAt"])
        counts, outbox = self.sweep()
        self.assertEqual((counts["sent"], outbox), (0, []))
        # A plain later visit never re-subscribes; pressing Tell me again does.
        self.record(at=self.now, notify=False)
        self.assertFalse(next(iter(self.leads().values()))["notifyWhenChanged"])
        self.assertEqual(client.post(url, {"token": "forged"}).status_code, 400)

    def test_the_flag_off_reads_and_sends_nothing(self):
        self.record()
        with override_settings(MY_STARTUP_WEBSITE_LEAD_REMINDERS_ENABLED=False):
            counts, outbox = self.sweep()
        self.assertEqual((counts, outbox), ({"skipped": "flag_off"}, []))

    def test_a_guidance_change_notifies_its_leads_once(self):
        self.record(at=self.now - timedelta(days=1))
        self.assertEqual(next(iter(self.leads().values()))["guidanceState"], "lovable:unverified")
        with patch.dict(reminders.GUIDANCE_STATES, {"lovable": "verified"}):
            counts, outbox = self.sweep()
            self.assertEqual(counts["sent"], 1, counts)
            self.assertIn("news", outbox[0]["payload"]["subject"])
            self.assertIn("Lovable", outbox[0]["payload"]["text"])
            counts, outbox = self.sweep(at=self.now + timedelta(hours=1))
            self.assertEqual(outbox, [])
        stored = next(iter(self.leads().values()))
        self.assertEqual(stored["guidanceState"], "lovable:verified")
        # Going back to what the founder already saw is not news; the
        # reminder is a separate, single email of its own.
        counts, outbox = self.sweep(at=self.now + timedelta(days=7))
        self.assertEqual(len(outbox), 1)
        self.assertIn("GitHub yet", outbox[0]["payload"]["subject"])

    def test_a_d9_change_reaches_no_export_leads(self):
        self.record(domain="acme.framer.website", hostClass="no_export", guidanceId="framer",
                    at=self.now - timedelta(days=1))
        with patch.dict(reminders.GUIDANCE_STATES, {"framer": "unverified"}):
            _counts, outbox = self.sweep()
        self.assertEqual(len(outbox), 1)
        self.assertIn("Framer", outbox[0]["payload"]["text"])

    def test_sends_are_capped_per_sweep(self):
        for index in range(reminders.SWEEP_CAP + 3):
            user = User.objects.create_user(email=f"f{index}@reminder.example.test", email_verified_at=self.now)
            self.record(user=user)
        counts, outbox = self.sweep()
        self.assertEqual((counts["sent"], len(outbox), counts["deferred"]), (reminders.SWEEP_CAP, reminders.SWEEP_CAP, 3))
        counts, outbox = self.sweep(at=self.now + timedelta(minutes=5))
        self.assertEqual(len(outbox), 3)
        counts, outbox = self.sweep(at=self.now + timedelta(minutes=10), limit=0)
        self.assertEqual(outbox, [])

    def test_a_retryable_failure_resends_with_the_same_key_and_a_refusal_never_resends(self):
        from system_email.transport_resend import SystemEmailRetryable
        self.record()
        with patch("system_email.transport_resend.send", side_effect=SystemEmailRetryable("resend_503")):
            counts = reminders.run_lead_reminder_sweep(now=self.now)
        self.assertEqual(counts["retry"], 1)
        key = next(iter(self.leads().values()))["reminder"]["key"]
        counts, outbox = self.sweep(at=self.now + timedelta(hours=1))
        self.assertEqual([item["key"] for item in outbox], [f"system-email/{key}"])
        counts, outbox = self.sweep(at=self.now + timedelta(hours=2))
        self.assertEqual(outbox, [])

    def test_a_claim_left_sending_past_the_window_is_closed_not_resent(self):
        from system_email.transport_resend import SystemEmailRetryable
        self.record()
        with patch("system_email.transport_resend.send", side_effect=SystemEmailRetryable("resend_503")):
            reminders.run_lead_reminder_sweep(now=self.now)
        _counts, outbox = self.sweep(at=self.now + reminders.RETRY_WINDOW + timedelta(minutes=1))
        self.assertEqual(outbox, [])
        self.assertEqual(next(iter(self.leads().values()))["reminder"]["status"], "failed")

    def test_unverified_changed_unsubscribed_or_connected_recipients_are_skipped(self):
        self.record()
        Contact.objects.filter(email=self.user.email).update(email_status=Contact.EmailStatus.UNSUBSCRIBED)
        counts, outbox = self.sweep()
        self.assertEqual((counts["skipped"], outbox), (1, []))
        self.assertEqual(next(iter(self.leads().values()))["reminder"]["status"], "skipped")

    def test_the_sweep_is_registered_with_the_scheduled_sweeps(self):
        from content_factory.self_heal_sweeps import SWEEPS
        self.assertIn(("website_lead_reminders", "founder_tools.website_lead_reminders", "run_lead_reminder_sweep"),
                      SWEEPS)

    def test_guidance_states_cover_every_builder_and_d9_builders_cannot_connect_yet(self):
        self.assertEqual(set(reminders.GUIDANCE_STATES), set(reminders.GUIDANCE_NAMES))
        for builder in ("framer", "webflow", "wix", "squarespace", "shopify", "chatgpt_sites"):
            self.assertEqual(reminders.GUIDANCE_STATES[builder], "cant_connect_yet")
        self.assertNotIn("verified", set(reminders.GUIDANCE_STATES.values()))

    def test_a_changed_email_an_inactive_account_or_a_connected_website_is_skipped(self):
        from content_factory.website_models import WebsiteConnection
        from organizations.models import Organization
        self.record()
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        counts, outbox = self.sweep()
        self.assertEqual((counts["skipped"], outbox), (1, []))

        moved = User.objects.create_user(email="moved@reminder.example.test", email_verified_at=self.now)
        self.record(user=moved)
        User.objects.filter(pk=moved.pk).update(email="elsewhere@reminder.example.test")
        counts, outbox = self.sweep()
        self.assertEqual((counts["skipped"], outbox), (1, []))

        connected = User.objects.create_user(email="connected@reminder.example.test", email_verified_at=self.now)
        self.record(user=connected)
        org = Organization.objects.create(domain="acme-connected.example.test", name="Acme", billing_user=connected)
        WebsiteConnection.objects.create(organization=org, github_repo="example/site")
        counts, outbox = self.sweep()
        self.assertEqual((counts["skipped"], outbox), (1, []))

    def test_a_cms_or_staff_lead_hears_only_about_changes_never_the_github_reminder(self):
        self.record(reason="cms_content", hostClass="wordpress", guidanceId=None, domain="acme.example.test")
        self.record(reason="setup_cant_be_done_yet", hostClass="unknown", guidanceId=None, domain="beta.example.test")
        counts, outbox = self.sweep()
        self.assertEqual((counts["sent"], outbox), (0, []))

    def test_an_opt_out_after_a_retryable_failure_stops_the_retry(self):
        from system_email.transport_resend import SystemEmailRetryable
        self.record()
        with patch("system_email.transport_resend.send", side_effect=SystemEmailRetryable("resend_503")):
            reminders.run_lead_reminder_sweep(now=self.now)
        contact = Contact.objects.get(email=self.user.email)
        self.assertEqual(reminders.opt_out(reminders.opt_out_token(contact)), "opted_out")
        _counts, outbox = self.sweep(at=self.now + timedelta(hours=1))
        self.assertEqual(outbox, [])
        self.assertEqual(next(iter(self.leads().values()))["reminder"]["status"], "skipped")

    def test_the_opt_out_token_carries_no_address_and_dies_with_an_email_change(self):
        import base64
        self.record()
        contact = Contact.objects.get(email=self.user.email)
        token = reminders.opt_out_token(contact)
        body = token.split(":")[0]
        decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        self.assertNotIn(b"reminder.example.test", decoded)
        Contact.objects.filter(pk=contact.pk).update(email="other@reminder.example.test")
        self.assertEqual(reminders.opt_out(token), "invalid")
