"""Invites, blasts, reminders and Ask Roo to draft, without a database."""

from datetime import datetime, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase, override_settings

from crm import blast_email, message_types, reminders, roo_draft
from crm.blasts import BlastError, check_channel_rules
from crm.models import Blast, CrmEvent, SenderMailbox

MELBOURNE = ZoneInfo("Australia/Melbourne")
UTC = dt_timezone.utc


def event(**values):
    defaults = dict(name="AI Builders Night", url="https://luma.com/abc123", cover_url="https://images.lumacdn.com/c.png",
                    start_at=datetime(2026, 11, 20, 18, 0, tzinfo=MELBOURNE), end_at=None,
                    timezone="Australia/Melbourne", visibility="public", approved_count=40, guest_count=45,
                    raw_payload={"geo_address_json": {"full_address": "Stone & Chalk, 121 King St, Melbourne"},
                                 "description_md": "Demos and drinks."},
                    reminder_settings={})
    defaults.update(values)
    return CrmEvent(**defaults)


def blast(kind, purpose, *, with_event=True, **values):
    item = Blast(message_type=kind, purpose=purpose, channels=["email"], **values)
    if with_event:
        item.event = event()
    return item


class RulesTests(SimpleTestCase):
    def test_an_invite_is_marketing_tied_to_one_event(self):
        check_channel_rules(blast("invite", "marketing"))
        for item, code in ((blast("invite", "event_update"), "invite_must_be_marketing"),
                           (blast("invite", "marketing", with_event=False), "event_required"),
                           (blast("invite", "marketing", quick_segment="going"), "invite_cannot_target_guests")):
            with self.subTest(code=code), self.assertRaises(BlastError) as caught:
                check_channel_rules(item)
            self.assertEqual(caught.exception.code, code)

    def test_a_blast_goes_to_guests_as_an_event_update(self):
        check_channel_rules(blast("blast", "event_update", quick_segment="going"))
        with self.assertRaises(BlastError) as caught:
            check_channel_rules(blast("blast", "marketing"))
        self.assertEqual(caught.exception.code, "blast_must_be_event_update")

    def test_general_marketing_keeps_working_without_a_type(self):
        check_channel_rules(blast("", "marketing", with_event=False))


class LayoutTests(SimpleTestCase):
    def test_invite_is_laid_out_like_a_luma_invite(self):
        body = message_types.layout(blast("invite", "marketing"), "Come along.", campaign="crm-blast-1")
        self.assertTrue(body.startswith("<table"))
        for text in ("You&rsquo;re invited to", "AI Builders Night", ">NOV<", ">20<", "Friday, 20 November",
                     "6:00 PM AEDT", "Stone &amp; Chalk, 121 King St, Melbourne", "Accept Invite", "View Event",
                     "Can&rsquo;t make it?", "Share with friends", "utm_campaign=crm-blast-1"):
            self.assertIn(text, body)
        # The message sits between two raw HTML blocks as ordinary Markdown.
        self.assertIn("\n\nCome along.\n\n</td></tr>", body)
        self.assertLess(body.index("Come along."), body.index("Accept Invite"))
        before, _, after = body.partition("\n\nCome along.\n\n")
        self.assertNotIn("\n\n", before)
        self.assertNotIn("\n\n", after)

    def test_invite_shows_the_end_time_and_a_hidden_or_online_place(self):
        item = blast("invite", "marketing")
        item.event.end_at = datetime(2026, 11, 20, 21, 0, tzinfo=MELBOURNE)
        item.event.raw_payload = {"geo_address_visibility": "guests-only",
                                  "geo_address_json": {"address": "Secret HQ", "city_state": "Melbourne, VIC"}}
        body = message_types.layout(item, "Hi", campaign="c")
        self.assertIn("6:00 PM – 9:00 PM AEDT", body)
        self.assertIn("Register to see location", body)
        self.assertNotIn("Secret HQ", body)
        item.event.raw_payload = {"meeting_url": "https://zoom.us/j/1"}
        body = message_types.layout(item, "Hi", campaign="c")
        self.assertIn("Online event", body)
        self.assertNotIn("zoom.us", body)

    def test_invite_without_a_public_link_has_no_buttons_or_sharing(self):
        item = blast("invite", "marketing")
        item.event.url = ""
        body = message_types.layout(item, "Hi", campaign="c")
        self.assertNotIn("Accept Invite", body)
        self.assertNotIn("Share with friends", body)

    def test_invite_buttons_are_tracked_and_share_links_are_not(self):
        body = message_types.layout(blast("invite", "marketing"), "See [the agenda](https://mlai.au/a).", campaign="c")
        tracked = blast_email.track_links(body)
        self.assertIn("utm_campaign=c@TrackLink", tracked)
        self.assertIn("(https://mlai.au/a@TrackLink)", tracked)
        self.assertNotIn("facebook.com/sharer/sharer.php?u=https%3A%2F%2Fluma.com%2Fabc123%3Futm_source%3Dmlai-crm%26utm_medium%3Dshare@TrackLink", tracked)
        self.assertEqual(tracked.count("data-notrack href"), 4)
        self.assertTrue(all("@TrackLink" not in part.split('"')[1] for part in tracked.split("data-notrack href=")[1:]))
        urls = blast_email.tracked_urls(tracked)
        self.assertIn("https://mlai.au/a", urls)
        self.assertTrue(any("utm_campaign=c" in url and "&amp;" not in url for url in urls))
        self.assertFalse(any("facebook.com" in url for url in urls))

    def test_blast_puts_the_message_first_and_a_compact_card_below(self):
        body = message_types.layout(blast("blast", "event_update"), "Doors open at 5:45.", campaign="c")
        self.assertTrue(body.startswith("Doors open at 5:45."))
        self.assertIn(">View event</a>", body)
        self.assertIn("background:#4f3a75", body)
        self.assertNotIn("![", body)

    def test_invite_retains_its_brand_and_secure_venue_in_both_formats(self):
        item = blast("invite", "marketing", body_markdown="See you there.")
        item.mailbox = SenderMailbox(email="hello@news.mlai.au", display_name="MLAI", reply_to="")
        item.event.raw_payload = {"geo_address_visibility": "guests-only",
                                  "geo_address_json": {"address": "Private HQ", "city_state": "Melbourne, VIC"}}
        payload = blast_email.campaign_payload(item, 123)
        self.assertIn("mlai-event-invite", payload["tags"])
        self.assertIn("mlai-roo.png", payload["body"])
        self.assertIn("Accept invite: https://luma.com/abc123", payload["altbody"])
        for part in ("body", "altbody"):
            self.assertIn("Register to see location", payload[part])
            self.assertNotIn("Private HQ", payload[part])

    def test_reconfirmation_button_keeps_the_go_url_in_both_formats(self):
        from crm.deliverability import reconfirmation

        item = blast("", "marketing", with_event=False, body_markdown="Still want to hear from us?",
                     stats={"crm_lifecycle": {"kind": "reconfirmation"}})
        item.mailbox = SenderMailbox(email="hello@news.mlai.au", display_name="MLAI", reply_to="")
        payload = blast_email.campaign_payload(item, 123)
        self.assertIn(f'href="{reconfirmation.GO_URL}"', payload["body"])
        self.assertIn("background:#4f3a75", payload["body"])
        self.assertIn(reconfirmation.GO_URL, payload["altbody"])

    def test_event_text_cannot_open_a_template_or_html(self):
        item = blast("blast", "event_update")
        item.event.name = "{{ .Subscriber.Email }} <script>"
        body = message_types.layout(item, "Hi", campaign="c")
        self.assertNotIn("{{", body)
        self.assertNotIn("<script>", body)
        invite = blast("invite", "marketing")
        invite.event.name = "{{ .Subscriber.Email }} <script>"
        invite.event.raw_payload = {"geo_address_json": {"address": "{{ x }} <b>", "full_address": "a\\n\\nb"}}
        body = message_types.layout(invite, "Hi", campaign="c")
        self.assertNotIn("{{", body)
        self.assertNotIn("<script>", body)
        self.assertNotIn("<b>", body)

    def test_general_marketing_is_left_alone(self):
        self.assertEqual(message_types.layout(blast("", "marketing"), "Hello", campaign="c"), "Hello")


class ReminderTimingTests(SimpleTestCase):
    def at(self, *args):
        return datetime(*args, tzinfo=MELBOURNE)

    def test_an_evening_event_gets_reminders_24_hours_and_1_hour_before(self):
        item = event(start_at=self.at(2026, 11, 20, 18, 0))
        self.assertEqual(reminders.send_time(item, "day_before"), self.at(2026, 11, 19, 18, 0))
        self.assertEqual(reminders.send_time(item, "hour_before"), self.at(2026, 11, 20, 17, 0))

    def test_an_early_morning_event_moves_both_reminders_into_sending_hours(self):
        item = event(start_at=self.at(2026, 11, 21, 7, 30))
        # 7:30am the day before is too early, so it goes at 8am that day; the
        # 1-hour reminder (6:30am) would land after the event starts, so it is skipped.
        self.assertEqual(reminders.send_time(item, "day_before"), self.at(2026, 11, 20, 8, 0))
        self.assertIsNone(reminders.send_time(item, "hour_before"))
        nine = event(start_at=self.at(2026, 11, 21, 9, 0))
        self.assertEqual(reminders.send_time(nine, "day_before"), self.at(2026, 11, 20, 9, 0))
        self.assertEqual(reminders.send_time(nine, "hour_before"), self.at(2026, 11, 21, 8, 0))

    def test_a_late_event_sends_no_later_than_8pm(self):
        item = event(start_at=self.at(2026, 11, 20, 21, 30))
        self.assertEqual(reminders.send_time(item, "hour_before"), self.at(2026, 11, 20, 20, 0))

    def test_an_early_event_skips_a_reminder_that_cannot_go_in_time(self):
        item = event(start_at=self.at(2026, 11, 21, 8, 10))
        self.assertIsNone(reminders.send_time(item, "hour_before"))

    def test_private_and_tiny_events_are_skipped_unless_staff_force_them(self):
        self.assertFalse(reminders.eligible(event(visibility="private"), reminders.settings_for(event())))
        tiny = event(approved_count=1, guest_count=1)
        self.assertFalse(reminders.eligible(tiny, reminders.settings_for(tiny)))
        forced = event(approved_count=1, guest_count=1, reminder_settings={"force": True})
        self.assertTrue(reminders.eligible(forced, reminders.settings_for(forced)))

    def test_fixed_text_includes_the_staff_note(self):
        item = event(reminder_settings={"note": "Enter via the side door"})
        self.assertEqual(message_types.reminder_subject(item, "day_before"),
                         "Reminder: AI Builders Night starts tomorrow")
        body = message_types.reminder_body(item, "hour_before", "Enter via the side door")
        self.assertIn("starts in 1 hour, at 6pm.", body)
        self.assertIn("Enter via the side door", body)
        self.assertTrue(body.startswith("Hi {{ first_name }},"))

    @override_settings(CRM_AUTO_REMINDERS_ENABLED=False)
    def test_the_runner_is_off_by_default(self):
        self.assertEqual(reminders.run_reminder_scheduler()["reason"], "disabled")


class FakeResponse:
    def __init__(self, text):
        self.output_text, self.output = text, []


class FakeClient:
    def __init__(self, text):
        self.text, self.calls = text, []
        self.responses = SimpleNamespace(create=self.create)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self.text)


class RooDraftTests(SimpleTestCase):
    def setUp(self):
        self.blast = blast("invite", "marketing", sign_off_name="Sam from the MLAI team")
        self.enterContext(patch("crm.roo_draft.past_examples", return_value=[]))

    def test_the_model_gets_event_facts_and_no_contact_data(self):
        payload = roo_draft.build_input(self.blast, "mention free pizza")
        self.assertEqual(payload["message_type"], "invite")
        self.assertEqual(payload["event"]["starts"], "Fri 20 Nov, 6pm")
        self.assertEqual(payload["untrusted_staff_note"], "mention free pizza")
        self.assertNotIn("@", str(payload).replace("{{ first_name }}", ""))

    def test_reminders_keep_their_fixed_text(self):
        with self.assertRaises(roo_draft.RooDraftError):
            roo_draft.build_input(blast("reminder", "event_update"), "")

    def test_a_clean_draft_passes(self):
        facts = roo_draft.build_input(self.blast, "")
        out = roo_draft.check_draft(self.blast, {
            "subject": "Join us for AI Builders Night", "preview_text": "Demos, drinks and builders",
            "body_markdown": "Hi {{ first_name }},\n\nCome see demos. [Register](https://luma.com/abc123)", "sms": "x",
        }, facts)
        self.assertEqual(out["sms"], "")  # SMS isn't selected.

    def test_drafts_that_break_the_rules_are_refused(self):
        facts = roo_draft.build_input(self.blast, "")
        base = {"subject": "Hi", "preview_text": "", "sms": ""}
        for body, reason in (("Hi {{ email }}", "merge_tags"),
                             ("See https://evil.example/x", "link"),
                             ("Tickets are $20", "price"),
                             ("![x](https://luma.com/abc123)", "image")):
            with self.subTest(reason=reason), self.assertRaises(roo_draft.RooDraftError) as caught:
                roo_draft.check_draft(self.blast, {**base, "body_markdown": body}, facts)
            self.assertEqual(caught.exception.extra.get("reason"), reason)

    def test_call_model_asks_for_structured_output(self):
        client = FakeClient('{"subject": "S", "preview_text": "P", "body_markdown": "B", "sms": ""}')
        result = roo_draft.call_model({"a": 1}, client=client)
        self.assertEqual(result["subject"], "S")
        self.assertEqual(client.calls[0]["text"]["format"]["type"], "json_schema")
        self.assertFalse(client.calls[0]["store"])

    @override_settings(CRM_ROO_DRAFT_ENABLED=False)
    def test_disabled_by_default(self):
        with self.assertRaises(roo_draft.RooDraftError) as caught:
            roo_draft.draft(self.blast, "", actor=None)
        self.assertEqual(caught.exception.code, "roo_draft_disabled")
