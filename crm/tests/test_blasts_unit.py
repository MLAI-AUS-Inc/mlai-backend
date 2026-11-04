"""Database-free checks for blasts: content for listmonk, status roll-up, audience shapes and the API layer."""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.urls import resolve
from rest_framework.exceptions import ValidationError

from community_chat.crm import serializers as wire
from community_chat.crm import views
from crm import blast_email, blasts, listmonk_sync, merge_tags
from crm.blasts import (
    BlastError,
    audience_body,
    channel_state,
    check_channel_rules,
    check_ready,
    mask_address,
    rollup_status,
)
from crm.listmonk import ListmonkError
from crm.models import (
    Blast,
    BlastDelivery,
    CrmEvent,
    InboundMessage,
    MessageTemplate,
    SenderMailbox,
    Contact,
)

PREFIX = "/api/v1/community-chat/crm/"
EVENT = CrmEvent(
    name="MLAI {Meetup}",
    luma_event_id="evt-1",
    url="https://luma.com/mlai",
    start_at=datetime(2026, 10, 30, 7, 30, tzinfo=timezone.utc),
    timezone="Australia/Melbourne",
)
Status = BlastDelivery.Status


def mailbox(**fields):
    values = {
        "email": "sam@mlai.au",
        "display_name": "Sam from MLAI",
        "domain": "mlai.au",
        "route": "google_relay",
        "reply_to": "hello@mlai.au",
        "enabled": True,
    }
    values.update(fields)
    return SenderMailbox(**values)


def template(**fields):
    values = {
        "name": "mlai_event_reminder",
        "content_sid": "HX1",
        "body": "Hi {{1}}, {{2}} starts {{3}}. {{4}}",
        "variables": [
            {"index": 1, "source": "first_name", "label": "Name"},
            {"index": 2, "source": "event_name", "label": "Event"},
            {"index": 4, "source": "custom", "label": "Details"},
        ],
        "approved": True,
        "enabled": True,
    }
    values.update(fields)
    return MessageTemplate(**values)


def draft(**fields):
    values = {
        "purpose": "event_update",
        "event": EVENT,
        "quick_segment": "going",
        "channels": ["email"],
        "mailbox": mailbox(),
        "subject": "Doors open at 6",
        "body_markdown": "Hi {{ first_name }}, see you at {{ event_name }}.",
    }
    values.update(fields)
    return Blast(**values)


class ListmonkContentTests(SimpleTestCase):
    def test_person_tags_become_go_expressions_and_event_tags_are_filled_in(self):
        body = blast_email.listmonk_text(
            "Hi {{ first_name }} ({{display_name}}): {{ event_name }} on {{ event_start_local }}, {{ event_url }}",
            EVENT,
            markdown=True,
        )
        self.assertEqual(
            body,
            "Hi {{ .Subscriber.FirstName }} ({{ .Subscriber.Name }}): MLAI &#123;Meetup} on "
            "Fri 30 Oct, 6:30pm, https://luma.com/mlai",
        )

    def test_braces_can_never_open_a_template_action(self):
        event = CrmEvent(name="{{ .Subscriber.Email }} <b>")
        body = blast_email.listmonk_text("{{ event_name }}{{ first_name }} {x}} {{", event, markdown=True)
        self.assertNotIn("{{ .Subscriber.Email", body)
        self.assertEqual(body.count("{{"), 1)
        self.assertIn("&lt;b&gt;", body)
        subject = blast_email.listmonk_text("{{ event_name }} {", event, markdown=False)
        self.assertEqual(subject, '{{ "{" }}{{ "{" }} .Subscriber.Email }} <b> {{ "{" }}')

    def test_unsupported_tags_are_refused(self):
        with self.assertRaises(merge_tags.UnsupportedMergeTag):
            blast_email.listmonk_text("Hi {{ .Subscriber.Email }}", EVENT, markdown=True)

    def test_markdown_links_are_tracked_once(self):
        body = blast_email.track_links(
            "[RSVP](https://luma.com/mlai) and [again]( https://luma.com/mlai ) and "
            "[done](https://x.test/a@TrackLink) and https://bare.test"
        )
        self.assertEqual(
            body,
            "[RSVP](https://luma.com/mlai@TrackLink) and [again](https://luma.com/mlai@TrackLink) and "
            "[done](https://x.test/a@TrackLink) and https://bare.test",
        )
        self.assertEqual(blast_email.tracked_urls(body), ["https://luma.com/mlai", "https://x.test/a"])

    def test_from_address_is_quoted_where_needed(self):
        self.assertEqual(blast_email.from_address("Sam from MLAI", "sam@mlai.au"), "Sam from MLAI <sam@mlai.au>")
        self.assertEqual(blast_email.from_address("Sam, MLAI", "sam@mlai.au"), '"Sam, MLAI" <sam@mlai.au>')
        self.assertEqual(blast_email.from_address("Sam\r\nBcc: x", "sam@mlai.au"), '"Sam Bcc: x" <sam@mlai.au>')
        self.assertEqual(blast_email.from_address("", "sam@mlai.au"), "sam@mlai.au")

    def test_subscriber_name_keeps_the_first_name_first(self):
        person = SimpleNamespace
        self.assertEqual(blast_email.subscriber_name(person(first_name="Ada", last_name="L", display_name="Ada Lovelace")), "Ada Lovelace")
        self.assertEqual(blast_email.subscriber_name(person(first_name="Ada", last_name="Lovelace", display_name="Countess Ada")), "Ada Lovelace")
        self.assertEqual(blast_email.subscriber_name(person(first_name="", last_name="", display_name="")), "there")

    @override_settings(LISTMONK_TEMPLATE_ID=7)
    def test_campaign_payload(self):
        blast = draft(body_markdown="Hi {{ first_name }}. [Details]({{ event_url }})")
        payload = blast_email.campaign_payload(blast, 12)
        self.assertEqual(payload["name"], f"crm-blast-{blast.pk}")
        self.assertEqual(payload["lists"], [12])
        self.assertEqual(payload["from_email"], "Sam from MLAI <sam@mlai.au>")
        self.assertEqual((payload["type"], payload["content_type"], payload["messenger"]), ("regular", "markdown", "email"))
        self.assertEqual(payload["tags"], ["crm", f"blast:{blast.pk}"])
        self.assertEqual(payload["headers"], [{"Reply-To": "hello@mlai.au"}])
        self.assertEqual(payload["template_id"], 7)
        self.assertEqual(payload["body"], "Hi {{ .Subscriber.FirstName }}. [Details](https://luma.com/mlai@TrackLink)")
        with override_settings(LISTMONK_TEMPLATE_ID=None):
            blast.mailbox.reply_to = ""
            payload = blast_email.campaign_payload(blast, 12)
        self.assertNotIn("template_id", payload)
        self.assertEqual(payload["headers"], [])

    def test_email_stats_merge_without_losing_keys(self):
        stats = blast_email.with_email_stats({"other": 1, "email": {"sent": 1}}, to_send=5)
        self.assertEqual(stats, {"other": 1, "email": {"sent": 1, "to_send": 5}})
        self.assertFalse(blast_email.email_started(Blast(stats=stats)))
        self.assertTrue(blast_email.email_started(Blast(stats={"email": {"started_at": "2026-10-23T01:00:00Z"}})))
        self.assertEqual(
            blast_email.parse_iso("2026-10-23T01:00:00Z"), datetime(2026, 10, 23, 1, tzinfo=timezone.utc)
        )
        self.assertIsNone(blast_email.parse_iso("yesterday"))

    def test_listmonk_opt_outs_are_read_from_the_subscriber(self):
        self.assertTrue(blast_email._opted_out_in_listmonk({"status": "blocklisted", "lists": []}))
        self.assertTrue(
            blast_email._opted_out_in_listmonk(
                {"status": "enabled", "lists": [{"name": "crm-blast-1", "subscription_status": "unsubscribed"}]}
            )
        )
        self.assertFalse(
            blast_email._opted_out_in_listmonk(
                {"status": "enabled", "lists": [{"name": "Other", "subscription_status": "unsubscribed"}]}
            )
        )

    @override_settings(CRM_SENDING_ENABLED=True, LISTMONK_BASE_URL="http://listmonk:9000", LISTMONK_API_USER="u", LISTMONK_API_TOKEN="t")
    def test_email_block_reasons(self):
        self.assertIsNone(blast_email.email_block(draft()))
        self.assertEqual(blast_email.email_block(draft(mailbox=mailbox(enabled=False))), "mailbox_disabled")
        with override_settings(LISTMONK_API_TOKEN=""):
            self.assertEqual(blast_email.email_block(draft()), "not_configured")
        with override_settings(CRM_SENDING_ENABLED=False):
            self.assertEqual(blast_email.email_block(draft()), "channel_disabled")


class StatusRollupTests(SimpleTestCase):
    def test_channel_state(self):
        self.assertEqual(channel_state({}), "done")
        self.assertEqual(channel_state({Status.QUEUED: 1, Status.SENT: 4}), "in_flight")
        self.assertEqual(channel_state({Status.SUBMITTING: 2}, blocked=True), "blocked")
        self.assertEqual(channel_state({Status.SENT: 3, Status.FAILED: 1}), "done")
        self.assertEqual(channel_state({Status.UNKNOWN: 1, Status.FAILED: 4}), "done")
        self.assertEqual(channel_state({Status.FAILED: 2}), "failed")
        # Everyone opted out before the send: nothing failed.
        self.assertEqual(channel_state({Status.UNSUBSCRIBED: 2}), "done")
        self.assertEqual(channel_state({Status.SENT: 2}, provider_failed=True), "failed")

    def test_rollup(self):
        self.assertEqual(rollup_status(["done", "in_flight"]), "sending")
        self.assertEqual(rollup_status(["done", "done"]), "sent")
        self.assertEqual(rollup_status(["done", "failed"]), "partially_sent")
        self.assertEqual(rollup_status(["blocked", "done"]), "partially_sent")
        self.assertEqual(rollup_status(["failed", "blocked"]), "failed")
        self.assertEqual(rollup_status([]), "failed")


class AudienceShapeTests(SimpleTestCase):
    def test_audience_body_matches_the_contract(self):
        body = audience_body(
            people=203,
            reachable={"email": 142, "sms": 21, "whatsapp": 40},
            excluded={
                "email": {"unsubscribed": 3, "bounced": 1, "banned": 0, "no_consent": 0, "not_registered": 0},
                "sms": {"no_mobile": 80, "covered_by_whatsapp": 40},
                "whatsapp": {"not_opted_in": 100, "unexpected": 9},
            },
            sms={"segments_per_message": 2, "encoding": "ucs2", "estimated_cost": Decimal("1.675")},
            whatsapp_cost=Decimal("3"),
            limits=[{"channel": "sms", "code": "daily_limit", "remaining_today": 10}],
        )
        self.assertEqual(body["people"], 203)
        self.assertEqual(
            body["channels"]["sms"],
            {
                "reachable": 21,
                "excluded": {"banned": 0, "opted_out": 0, "no_mobile": 80, "not_allowed_country": 0, "covered_by_whatsapp": 40},
                "segments_per_message": 2,
                "encoding": "ucs2",
                "estimated_cost": {"amount": "1.68", "currency": "AUD"},
            },
        )
        self.assertEqual(
            body["channels"]["whatsapp"],
            {
                "reachable": 40,
                "excluded": {"banned": 0, "no_phone": 0, "not_opted_in": 100},
                "estimated_cost": {"amount": "3.00", "currency": "AUD"},
            },
        )
        self.assertEqual(set(body["channels"]["email"]["excluded"]), set(blasts.EXCLUDED["email"]))
        self.assertEqual(body["limits"][0]["remaining_today"], 10)

    def test_addresses_are_masked(self):
        self.assertEqual(mask_address("email", "sam@mlai.au"), "s•••@mlai.au")
        self.assertEqual(mask_address("sms", "+61412345678"), "+61•••678")
        self.assertEqual(mask_address("whatsapp", "+6141"), "•••")
        self.assertEqual(mask_address("email", "nonsense"), "•••")


class DraftRuleTests(SimpleTestCase):
    def code(self, blast, channels=None, ready=True):
        with self.assertRaises(BlastError) as caught:
            (check_ready(blast, channels) if ready else check_channel_rules(blast))
        return caught.exception.code, caught.exception.extra

    def test_sms_and_whatsapp_are_event_blasts_only(self):
        self.assertEqual(
            self.code(draft(purpose="marketing", channels=["email", "sms"]), ready=False),
            ("channel_not_allowed", {"channel": "sms"}),
        )
        self.assertEqual(self.code(draft(event=None, quick_segment="", channels=["email"]), ready=False)[0], "event_required")

    def test_merge_tags_are_checked_per_field(self):
        self.assertEqual(
            self.code(draft(subject="Hi {{ email }}"), ready=False),
            ("unsupported_merge_tag", {"tags": ["email"], "field": "subject"}),
        )
        marketing = draft(purpose="marketing", event=None, quick_segment="", body_markdown="{{ event_url }}")
        self.assertEqual(self.code(marketing, ready=False), ("merge_tag_needs_event", {"field": "body_markdown"}))

    def test_email_needs_a_mailbox_subject_and_body(self):
        self.assertEqual(self.code(draft(mailbox=None))[0], "mailbox_required")
        self.assertEqual(self.code(draft(mailbox=mailbox(enabled=False)))[0], "mailbox_disabled")
        self.assertEqual(self.code(draft(subject=" "))[0], "subject_required")
        self.assertEqual(self.code(draft(body_markdown=""))[0], "body_required")
        self.assertEqual(self.code(draft(channels=[]))[0], "channels_required")
        check_ready(draft())

    def test_sms_and_whatsapp_parts(self):
        self.assertEqual(self.code(draft(channels=["sms"]))[0], "sms_body_required")
        check_ready(draft(channels=["sms"], sms_body="Doors open at 6"))
        self.assertEqual(self.code(draft(channels=["whatsapp"]))[0], "template_required")
        self.assertEqual(
            self.code(draft(channels=["whatsapp"], whatsapp_template=template(approved=False)))[0],
            "template_unavailable",
        )
        self.assertEqual(
            self.code(draft(channels=["whatsapp"], whatsapp_template=template()))[1], {"index": "4"}
        )
        check_ready(draft(channels=["whatsapp"], whatsapp_template=template(), whatsapp_variables={"4": "Level 3"}))
        # Only the channels being tested are checked.
        check_ready(draft(channels=["email", "sms"]), ["email"])


class RunnerGateTests(SimpleTestCase):
    @override_settings(CRM_SENDING_ENABLED=False, CRM_SMS_ENABLED=False, CRM_WHATSAPP_ENABLED=False)
    def test_dispatch_is_off_until_a_channel_is_on(self):
        with patch.object(blasts, "claim_blasts") as claim:
            self.assertEqual(blasts.run_blast_dispatch_scheduler(), {"status": "skipped", "reason": "disabled"})
        claim.assert_not_called()

    @override_settings(CRM_SMS_ENABLED=True, CRM_BLAST_DISPATCH_INTERVAL_SECONDS=60)
    def test_dispatch_is_throttled_and_only_rejected_credentials_fail(self):
        with patch.object(blasts.cache, "add", return_value=False):
            self.assertEqual(blasts.run_blast_dispatch_scheduler()["reason"], "throttled")
        first, second = uuid.uuid4(), uuid.uuid4()
        with (
            patch.object(blasts.cache, "add", return_value=True),
            patch.object(blasts, "claim_blasts", return_value=[first, second]),
            patch.object(blasts, "dispatch_blast", side_effect=[RuntimeError("bug"), {"status": "sending", "twilio_sends": 3}]),
            patch.object(blasts.Blast.objects, "filter"),
        ):
            result = blasts.run_blast_dispatch_scheduler()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["errors"], [str(first)])
        self.assertEqual(result["outcomes"], {str(second): "sending"})
        self.assertNotIn("failed", result)
        with (
            patch.object(blasts.cache, "add", return_value=True),
            patch.object(blasts, "claim_blasts", return_value=[first]),
            patch.object(blasts, "dispatch_blast", return_value={"status": "sending", "twilio_sends": 0, "deferred": "timeout"}),
        ):
            self.assertEqual(blasts.run_blast_dispatch_scheduler()["status"], "deferred")
        with (
            patch.object(blasts.cache, "add", return_value=True),
            patch.object(blasts, "claim_blasts", return_value=[first]),
            patch.object(blasts, "dispatch_blast", side_effect=ListmonkError("http_401", 401)),
        ):
            self.assertEqual(blasts.run_blast_dispatch_scheduler()["status"], "failed")

    @override_settings(LISTMONK_BASE_URL="", LISTMONK_API_USER="", LISTMONK_API_TOKEN="")
    def test_listmonk_sync_needs_listmonk(self):
        self.assertEqual(listmonk_sync.run_listmonk_sync_scheduler(), {"status": "skipped", "reason": "not_configured"})

    @override_settings(LISTMONK_BASE_URL="http://listmonk:9000", LISTMONK_API_USER="u", LISTMONK_API_TOKEN="t")
    def test_listmonk_sync_failure_semantics(self):
        cases = [
            (ListmonkError("timeout"), "deferred"),
            (ListmonkError("http_502", 502), "deferred"),
            (ListmonkError("http_400", 400), "deferred"),
            (ListmonkError("http_403", 403), "failed"),
        ]
        for error, status in cases:
            with self.subTest(code=error.code):
                with (
                    patch.object(listmonk_sync.cache, "add", return_value=True),
                    patch.object(listmonk_sync, "sync_listmonk", side_effect=error),
                ):
                    self.assertEqual(listmonk_sync.run_listmonk_sync_scheduler()["status"], status)
        with (
            patch.object(listmonk_sync.cache, "add", return_value=True),
            patch.object(listmonk_sync, "sync_listmonk", return_value={"opened": 2, "blasts_with_errors": []}),
        ):
            self.assertEqual(listmonk_sync.run_listmonk_sync_scheduler(), {"status": "completed", "opened": 2, "blasts_with_errors": []})
        with patch.object(listmonk_sync.cache, "add", return_value=False):
            self.assertEqual(listmonk_sync.run_listmonk_sync_scheduler()["reason"], "throttled")


class BlastApiShapeTests(SimpleTestCase):
    def test_blast_input_is_strict(self):
        def errors(data, partial=False):
            serializer = wire.BlastInput(data=data, partial=partial)
            self.assertFalse(serializer.is_valid())
            return serializer.errors

        self.assertIn("surprise", errors({"purpose": "marketing", "surprise": 1}))
        self.assertIn("unknown", errors({"email": {"unknown": "x"}})["email"])
        conflict = errors({"quick_segment": "going", "segment_id": str(uuid.uuid4())})
        self.assertEqual(conflict["segment_id"][0].code, "audience_conflict")
        slots = errors({"whatsapp": {"variables": {"x": "y"}}}, partial=True)
        self.assertEqual(slots["whatsapp"]["variables"][0].code, "invalid_slot")
        valid = wire.BlastInput(
            data={"channels": ["sms", "sms", "email"], "whatsapp": {"variables": {"04": "  Level   3 "}}},
            partial=True,
        )
        self.assertTrue(valid.is_valid(), valid.errors)
        self.assertEqual(valid.validated_data["channels"], ["sms", "email"])
        self.assertEqual(valid.validated_data["whatsapp"]["variables"], {"4": "Level 3"})

    def test_send_input_confirms_channels(self):
        serializer = wire.BlastSendInput(data={"confirm": {"fax": 1}, "idempotency_key": "k"})
        self.assertFalse(serializer.is_valid())
        serializer = wire.BlastSendInput(data={"confirm": {"email": 142, "sms": 0}, "idempotency_key": "k-1"})
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_blast_detail_and_progress(self):
        blast = draft(
            channels=["email", "sms"],
            sms_body="Doors open at 6",
            status="sending",
            stats={"email": {"listmonk_status": "running", "sent": 40, "to_send": 142}},
        )
        stats = {"channels": {"sms": {"recipients": 3, "sent": 2, "pending": 1}}, "replies": 2}
        body = wire.blast_detail(blast, stats)
        self.assertEqual(body["progress"], {"email": {"sent": 40, "to_send": 142}, "sms": {"sent": 2, "to_send": 3}})
        self.assertEqual(body["email"]["mailbox_id"], str(blast.mailbox_id))
        self.assertEqual(body["sms"], {"body": "Doors open at 6"})
        self.assertEqual(body["whatsapp"], {"template_id": None, "variables": {}})
        self.assertEqual(body["quick_segment"], "going")
        self.assertEqual(body["replies"], 2)
        self.assertEqual(body["audience"]["label"], "Going · MLAI {Meetup}")

    def test_delivery_reply_mailbox_and_template_rows(self):
        contact = Contact(email="ada@example.com", first_name="Ada", last_name="Lovelace")
        delivery = BlastDelivery(contact=contact, channel="email", address="ada@example.com", status="sent")
        row = wire.delivery_row(delivery)
        self.assertEqual((row["address"], row["display_name"], row["error_code"]), ("a•••@example.com", "Ada Lovelace", None))
        reply = wire.reply_row(InboundMessage(channel="sms", body="See you there", received_at=EVENT.start_at))
        self.assertEqual((reply["contact_id"], reply["received_at"]), (None, "2026-10-30T07:30:00Z"))
        self.assertEqual(wire.mailbox_dto(mailbox(daily_limit=500), 520)["remaining_today"], 0)
        dto = wire.template_dto(template())
        self.assertEqual(dto["variables"][2], {"index": 4, "source": "custom", "label": "Details"})
        self.assertEqual(dto["category"], "utility")

    def test_list_filters(self):
        event_id = str(uuid.uuid4())
        self.assertEqual(
            wire.blast_list_filters({"status": "sent", "channel": "sms", "event_id": event_id}),
            {"status": "sent", "channel": "sms", "event_id": event_id},
        )
        for params in ({"status": "done"}, {"channel": "fax"}, {"event_id": "x"}):
            with self.subTest(params=params), self.assertRaises(views.CrmError):
                wire.blast_list_filters(params)
        with self.assertRaises(views.CrmError):
            wire.delivery_filters({"status": "lost"})

    def test_blast_errors_keep_the_error_shape(self):
        handle = views.CrmView().handle_exception
        cases = [
            (BlastError("channel_disabled", 403, channel="sms", reason="disabled"), 403,
             {"error": "channel_disabled", "outcome": "channel_disabled", "channel": "sms", "reason": "disabled"}),
            (BlastError("count_changed", 409, people=3, channels={}), 409,
             {"error": "count_changed", "outcome": "count_changed", "people": 3, "channels": {}}),
            (merge_tags.UnsupportedMergeTag(["email"]), 400,
             {"error": "unsupported_merge_tag", "outcome": "unsupported_merge_tag", "tags": ["email"]}),
            (ListmonkError("timeout"), 503,
             {"error": "listmonk_unavailable", "outcome": "listmonk_unavailable", "detail": "timeout"}),
            (ListmonkError("http_400", 400), 502,
             {"error": "listmonk_rejected", "outcome": "listmonk_rejected", "detail": "http_400"}),
        ]
        for exc, status, body in cases:
            with self.subTest(code=exc.code):
                response = handle(exc)
                self.assertEqual((response.status_code, response.data), (status, body))
        with self.assertRaises(ValidationError):
            wire.BlastTestInput(data={"channels": []}).is_valid(raise_exception=True)

    def test_routes(self):
        blast_id = uuid.uuid4()
        for path, view in (
            ("mailboxes/", views.MailboxesView),
            ("whatsapp-templates/", views.WhatsAppTemplatesView),
            ("blasts/", views.BlastsView),
            (f"blasts/{blast_id}/", views.BlastsView),
            (f"blasts/{blast_id}/audience/", views.BlastAudienceView),
            (f"blasts/{blast_id}/test/", views.BlastTestView),
            (f"blasts/{blast_id}/send/", views.BlastSendView),
            (f"blasts/{blast_id}/cancel/", views.BlastCancelView),
            (f"blasts/{blast_id}/deliveries/", views.BlastDeliveriesView),
            (f"blasts/{blast_id}/replies/", views.BlastRepliesView),
        ):
            with self.subTest(path=path):
                self.assertIs(resolve(PREFIX + path).func.view_class, view)


class BlastErrorExtrasTests(SimpleTestCase):
    def test_a_blast_state_extra_never_collides_with_the_http_status(self):
        error = blasts.BlastError("blast_not_draft", 409, blast_status="sent")
        self.assertEqual((error.code, error.status, error.extra), ("blast_not_draft", 409, {"blast_status": "sent"}))
