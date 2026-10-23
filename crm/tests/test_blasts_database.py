"""PostgreSQL-backed checks for blasts (steps B4 and B5): queueing, dispatch, cancel and listmonk feedback.

Nothing leaves the process: listmonk and Twilio are one fake behind a mocked
``integrations.http_client.request``.
"""

import copy
import itertools
import json
import re
import uuid
from collections import defaultdict
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from crm import blasts, listmonk_sync
from crm.models import (
    Blast,
    BlastDelivery,
    Contact,
    ContactConsentEvent,
    CrmAuditEvent,
    InboundMessage,
    MessageTemplate,
    SenderMailbox,
)

from .api_fixtures import PREFIX, CrmApiMixin, make_contact, make_event, register

LISTMONK_URL = "http://listmonk.test:9000"
SENDING = {
    "COMMUNITY_CHAT_CRM_ENABLED": True,
    "CRM_LUMA_SYNC_ENABLED": False,
    "CRM_SENDING_ENABLED": True,
    "LISTMONK_BASE_URL": LISTMONK_URL,
    "LISTMONK_API_USER": "mlai-backend",
    "LISTMONK_API_TOKEN": "synthetic-token",
    "LISTMONK_TEMPLATE_ID": 3,
    "CRM_SMS_ENABLED": True,
    "CRM_WHATSAPP_ENABLED": True,
    "TWILIO_ACCOUNT_SID": "AC-test",
    "TWILIO_AUTH_TOKEN": "twilio-auth-token",
    "TWILIO_MESSAGING_SERVICE_SID": "MG-test",
    "TWILIO_WHATSAPP_FROM": "+61480000000",
    "DEFAULT_BACKEND_URL": "https://api.test",
    "CRM_SMS_ALLOWED_PREFIXES": ["+614"],
    "CRM_SMS_DAILY_LIMIT": 500,
    "CRM_WHATSAPP_DAILY_LIMIT": 500,
    "CRM_TWILIO_SENDS_PER_TICK": 200,
    "CRM_BLAST_DISPATCH_INTERVAL_SECONDS": 60,
    "CRM_LISTMONK_SYNC_INTERVAL_SECONDS": 300,
}


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def ok(data):
    return FakeResponse(200, {"data": data})


def page(results):
    return ok({"results": results, "total": len(results), "page": 1, "per_page": len(results)})


class FakeProviders:
    """An in-memory listmonk, plus Twilio's Messages API, for ``http_client.request``."""

    def __init__(self):
        self.ids = itertools.count(100)
        self.lists = {}
        self.subscribers = {}
        self.campaigns = {}
        self.calls = []
        self.tests = []
        self.twilio = []
        self.failures = []
        self.views = defaultdict(set)
        self.link_clicks = defaultdict(set)
        self.campaign_bounces = defaultdict(set)
        self.complaints = set()
        self.bounced = set()

    # Test controls

    def fail_next(self, method, pattern, status=503):
        self.failures.append((method, re.compile(pattern), status))

    def count(self, method, pattern):
        return sum(1 for call in self.calls if call[0] == method and re.fullmatch(pattern, call[1]))

    def add_subscriber(self, email, name="Someone", lists=(), status="enabled"):
        subscriber_id = next(self.ids)
        self.subscribers[subscriber_id] = {
            "id": subscriber_id,
            "email": email.lower(),
            "name": name,
            "status": status,
            "attribs": {},
            "lists": [],
        }
        for list_id in lists:
            self.join(subscriber_id, list_id)
        return self.subscribers[subscriber_id]

    def subscriber(self, email):
        return next(item for item in self.subscribers.values() if item["email"] == email)

    def join(self, subscriber_id, list_id, status="confirmed"):
        memberships = self.subscribers[subscriber_id]["lists"]
        if not any(item["id"] == list_id for item in memberships):
            memberships.append({"id": list_id, "name": self.lists.get(list_id, ""), "subscription_status": status})

    def unsubscribe(self, email, list_id):
        for item in self.subscriber(email)["lists"]:
            if item["id"] == list_id:
                item["subscription_status"] = "unsubscribed"

    def on_list(self, list_id):
        return {
            item["email"]
            for item in self.subscribers.values()
            if any(membership["id"] == list_id for membership in item["lists"])
        }

    # HTTP

    def __call__(self, method, url, **kwargs):
        if url.startswith("https://api.twilio.com/"):
            self.twilio.append(dict(kwargs.get("data") or {}))
            return FakeResponse(201, {"sid": f"SM{len(self.twilio):032d}"})
        assert url.startswith(LISTMONK_URL), url
        assert kwargs.get("auth") == ("mlai-backend", "synthetic-token")
        path = url[len(LISTMONK_URL):]
        self.calls.append((method, path))
        for failure in list(self.failures):
            if failure[0] == method and failure[1].fullmatch(path):
                self.failures.remove(failure)
                return FakeResponse(failure[2], {"message": "synthetic failure"})
        return self.route(method, path, kwargs.get("params") or {}, kwargs.get("json"))

    def route(self, method, path, params, body):
        if (method, path) == ("POST", "/api/lists"):
            list_id = next(self.ids)
            self.lists[list_id] = body["name"]
            return ok({"id": list_id, "name": body["name"]})
        if (method, path) == ("GET", "/api/lists"):
            return page([{"id": key, "name": name} for key, name in self.lists.items() if params["query"] in name])
        if (method, path) == ("GET", "/api/subscribers"):
            emails = self.emails_for(params["query"])
            return page([copy.deepcopy(item) for item in self.subscribers.values() if item["email"] in emails])
        if (method, path) == ("POST", "/api/subscribers"):
            if any(item["email"] == body["email"].lower() for item in self.subscribers.values()):
                return FakeResponse(409, {"message": "E-mail already exists."})
            return ok(copy.deepcopy(self.add_subscriber(body["email"], body["name"], body["lists"])))
        if (method, path) == ("PUT", "/api/subscribers/lists"):
            for subscriber_id in body["ids"]:
                for list_id in body["target_list_ids"]:
                    self.join(subscriber_id, list_id)
            return ok(True)
        if (method, path) == ("PUT", "/api/subscribers/blocklist"):
            for subscriber_id in body["ids"]:
                item = self.subscribers[subscriber_id]
                item["status"] = "blocklisted"
                for membership in item["lists"]:
                    membership["subscription_status"] = "unsubscribed"
            return ok(True)
        match = re.fullmatch(r"/api/subscribers/(\d+)", path)
        if method == "PUT" and match:
            item = self.subscribers[int(match.group(1))]
            kept = {membership["id"]: membership for membership in item["lists"]}
            item["name"] = body["name"]
            item["lists"] = []
            for list_id in body["lists"]:
                item["lists"].append(kept.get(list_id) or {"id": list_id, "name": self.lists.get(list_id, ""), "subscription_status": "confirmed"})
            return ok(copy.deepcopy(item))
        if (method, path) == ("POST", "/api/campaigns"):
            campaign_id = next(self.ids)
            self.campaigns[campaign_id] = {**body, "id": campaign_id, "status": "draft", "sent": 0, "to_send": 0, "views": 0, "clicks": 0}
            return ok(dict(self.campaigns[campaign_id]))
        if (method, path) == ("GET", "/api/campaigns"):
            return page([dict(item) for item in self.campaigns.values() if params["query"] in item["name"]])
        match = re.fullmatch(r"/api/campaigns/(\d+)(/status|/test)?", path)
        if match:
            campaign = self.campaigns.get(int(match.group(1)))
            if campaign is None:
                return FakeResponse(404, {"message": "Campaign not found."})
            if match.group(2) == "/status":
                wanted = body["status"]
                allowed = {"running": ("draft", "paused"), "cancelled": ("running",), "paused": ("running",)}
                if campaign["status"] not in allowed.get(wanted, ()):
                    return FakeResponse(400, {"message": "Invalid status change."})
                campaign["status"] = wanted
                return ok(dict(campaign))
            if match.group(2) == "/test":
                self.tests.append((campaign["id"], body))
                return ok(True)
            if method == "PUT":
                if campaign["status"] != "draft":
                    return FakeResponse(400, {"message": "Campaign can't be edited."})
                campaign.update(body)
                return ok(dict(campaign))
            return ok(dict(campaign))
        raise AssertionError(f"Unexpected listmonk call {method} {path}")

    def emails_for(self, query):
        """Answer the subscriber ``query`` expressions crm sends, by shape."""

        found = re.search(r"campaign_id = (\d+)", query)
        campaign_id = int(found.group(1)) if found else None
        everyone = list(self.subscribers.values())
        blocklisted = {item["email"] for item in everyone if item["status"] == "blocklisted"}
        if "campaign_views" in query:
            return set(self.views[campaign_id])
        if "link_clicks" in query:
            url = re.search(r"url = '((?:[^']|'')*)'", query)
            if url:
                return set(self.link_clicks[(campaign_id, url.group(1).replace("''", "'"))])
            return set().union(*[emails for (key, _), emails in self.link_clicks.items() if key == campaign_id])
        if "FROM bounces WHERE campaign_id" in query:
            return set(self.campaign_bounces[campaign_id])
        if "subscriber_lists WHERE list_id" in query:
            list_id = int(re.search(r"list_id = (\d+)", query).group(1))
            return {
                item["email"]
                for item in everyone
                if item["status"] != "blocklisted"
                and any(m["id"] == list_id and m["subscription_status"] == "unsubscribed" for m in item["lists"])
            }
        if "JOIN lists" in query:
            return {
                item["email"]
                for item in everyone
                if item["status"] != "blocklisted"
                and any(
                    m["subscription_status"] == "unsubscribed" and m["name"].startswith("crm-blast-")
                    for m in item["lists"]
                )
            }
        if "type = 'complaint'" in query:
            return blocklisted & self.complaints
        if "FROM bounces WHERE type IN" in query:
            return blocklisted & self.bounced
        if "subscribers.status = 'blocklisted'" in query:
            return blocklisted
        ids = re.search(r"subscribers\.id IN \(([\d,]+)\)", query)
        wanted = {int(value) for value in ids.group(1).split(",")} if ids else set()
        literals = {value.replace("''", "'") for value in re.findall(r"'((?:[^']|'')*)'", query)}
        matched = {item["email"] for item in everyone if item["id"] in wanted or item["email"] in literals}
        if "!= 'blocklisted'" in query:
            matched -= blocklisted
        return matched


class BlastFixtures(CrmApiMixin):
    """One upcoming event with five going guests and a waitlisted one."""

    def setUp(self):
        super().setUp()
        self.fake = FakeProviders()
        self.enterContext(patch("integrations.http_client.request", new=self.fake))
        self.now = timezone.now()
        self.event = make_event("evt-blast", start=self.now + timedelta(days=7), name="MLAI Meetup")
        self.mailbox = SenderMailbox.objects.create(
            email="sam@mlai.au",
            display_name="Sam from MLAI",
            domain="mlai.au",
            route=SenderMailbox.Route.GOOGLE_RELAY,
            reply_to="hello@mlai.au",
            daily_limit=500,
            enabled=True,
        )
        self.template = MessageTemplate.objects.create(
            name="mlai_event_reminder",
            content_sid="HX1",
            body="Hi {{1}}, {{2}} starts soon. {{3}}. Reply STOP to opt out.",
            variables=[
                {"index": 1, "source": "first_name", "label": "Name"},
                {"index": 2, "source": "event_name", "label": "Event"},
                {"index": 3, "source": "custom", "label": "Details"},
            ],
            approved=True,
            enabled=True,
        )
        # Ada: every channel (WhatsApp covers her SMS). Bob: email and SMS.
        # Cat: opted out everywhere. Dan: a New Zealand mobile. Eve: email only.
        self.ada = make_contact(
            "ada@example.com", first_name="Ada", phone_e164="+61412345671",
            sms_status="active", whatsapp_opt_in="granted",
        )
        self.bob = make_contact("bob@example.com", first_name="Bob", phone_e164="+61412345672", sms_status="active")
        self.cat = make_contact(
            "cat@example.com", email_status="unsubscribed", phone_e164="+61412345673", sms_status="opted_out"
        )
        self.dan = make_contact("dan@example.com", first_name="Dan", phone_e164="+64211234567", sms_status="active")
        self.eve = make_contact("eve@example.com", first_name="Eve")
        self.fay = make_contact("fay@example.com", first_name="Fay")
        for person in (self.ada, self.bob, self.cat, self.dan, self.eve):
            register(self.event, person)
        register(self.event, self.fay, "waitlist")

    def draft_body(self, **overrides):
        body = {
            "purpose": "event_update",
            "event_id": str(self.event.pk),
            "quick_segment": "going",
            "channels": ["email", "sms", "whatsapp"],
            "sms_only_without_whatsapp": True,
            "email": {
                "mailbox_id": str(self.mailbox.pk),
                "subject": "Doors open at 6, {{ first_name }}",
                "body_markdown": "Hi {{ first_name }}, see you at {{ event_name }}. [Details]({{ event_url }})",
            },
            "sms": {"body": "Hi {{ first_name }}, doors open at 6."},
            "whatsapp": {"template_id": str(self.template.pk), "variables": {"3": "Doors open at 6"}},
        }
        body.update(overrides)
        return body

    def create_draft(self, **overrides):
        response = self.client.post(PREFIX + "blasts/", self.draft_body(**overrides), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return Blast.objects.get(pk=response.json()["id"])

    def confirm(self, **counts):
        return {"email": 4, "sms": 1, "whatsapp": 1, **counts}

    def send(self, blast, confirm=None, key="key-1", client=None):
        return (client or self.client).post(
            PREFIX + f"blasts/{blast.pk}/send/",
            {"confirm": confirm or self.confirm(), "idempotency_key": key},
            format="json",
        )

    def queued(self, confirm=None, key="key-1", **overrides):
        blast = self.create_draft(**overrides)
        response = self.send(blast, confirm=confirm, key=key)
        self.assertEqual(response.status_code, 202, response.content)
        blast.refresh_from_db()
        return blast

    def finish_campaign(self, blast):
        blast.refresh_from_db()
        campaign = self.fake.campaigns[blast.listmonk_campaign_id]
        campaign.update(status="finished", sent=4, to_send=4, views=2, clicks=1)

    def statuses(self, blast, channel):
        return dict(
            BlastDelivery.objects.filter(blast=blast, channel=channel).values_list("contact__email", "status")
        )


@override_settings(**SENDING)
class BlastQueueDatabaseTests(BlastFixtures, TestCase):
    def test_audience_counts_each_channel_and_its_exclusions(self):
        blast = self.create_draft()
        body = self.client.post(PREFIX + f"blasts/{blast.pk}/audience/").json()
        self.assertEqual(body["people"], 4)
        email, sms, whatsapp = (body["channels"][name] for name in ("email", "sms", "whatsapp"))
        self.assertEqual(email["reachable"], 4)
        self.assertEqual(email["excluded"], {"unsubscribed": 1, "bounced": 0, "banned": 0, "no_consent": 0, "not_registered": 0})
        self.assertEqual(sms["reachable"], 1)
        self.assertEqual(
            sms["excluded"],
            {"banned": 0, "opted_out": 1, "no_mobile": 1, "not_allowed_country": 1, "covered_by_whatsapp": 1},
        )
        self.assertEqual((sms["segments_per_message"], sms["encoding"]), (1, "gsm7"))
        self.assertEqual(whatsapp["reachable"], 1)
        self.assertEqual(whatsapp["excluded"], {"banned": 0, "no_phone": 1, "not_opted_in": 3})
        self.assertEqual(body["limits"], [])

        self.client.patch(PREFIX + f"blasts/{blast.pk}/", {"sms_only_without_whatsapp": False}, format="json")
        body = self.client.post(PREFIX + f"blasts/{blast.pk}/audience/").json()
        self.assertEqual(body["channels"]["sms"]["reachable"], 2)
        self.assertEqual(body["channels"]["sms"]["excluded"]["covered_by_whatsapp"], 0)

    def test_send_snapshots_one_delivery_per_person_per_channel(self):
        blast = self.queued()
        self.assertEqual((blast.status, blast.recipient_count), ("queued", 4))
        self.assertIsNotNone(blast.queued_at)
        rows = BlastDelivery.objects.filter(blast=blast)
        self.assertEqual(
            sorted(rows.values_list("channel", "address")),
            [
                ("email", "ada@example.com"),
                ("email", "bob@example.com"),
                ("email", "dan@example.com"),
                ("email", "eve@example.com"),
                ("sms", "+61412345672"),
                ("whatsapp", "+61412345671"),
            ],
        )
        self.assertEqual(set(rows.values_list("consent_basis", flat=True)), {"event_registration"})
        self.assertEqual(set(rows.values_list("status", flat=True)), {"queued"})
        self.assertEqual(blast.segment_definition["rules"][0]["event_ids"], [str(self.event.pk)])
        audit = CrmAuditEvent.objects.get(action="send", target_id=str(blast.pk))
        self.assertEqual(audit.detail, {"operation": "queue", "channels": {"email": 4, "sms": 1, "whatsapp": 1}, "recipient_count": 4})
        self.assertEqual(audit.actor, self.admin)

    def test_send_is_idempotent_on_its_key(self):
        blast = self.queued()
        again = self.send(blast)
        self.assertEqual((again.status_code, again.json()["id"]), (202, str(blast.pk)))
        self.assertEqual(BlastDelivery.objects.filter(blast=blast).count(), 6)
        self.assertEqual(CrmAuditEvent.objects.filter(action="send", target_id=str(blast.pk)).count(), 1)

        other = self.create_draft()
        reused = self.send(other)
        self.assertEqual((reused.status_code, reused.json()["error"]), (409, "idempotency_key_reused"))
        not_draft = self.send(blast, key="key-2")
        self.assertEqual((not_draft.status_code, not_draft.json()["error"]), (409, "blast_not_draft"))
        patched = self.client.patch(PREFIX + f"blasts/{blast.pk}/", {"sms": {"body": "Late"}}, format="json")
        self.assertEqual((patched.status_code, patched.json()["error"]), (409, "blast_not_draft"))

    def test_a_count_change_answers_409_with_fresh_counts(self):
        blast = self.create_draft()
        response = self.send(blast, confirm=self.confirm(email=5))
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["error"], "count_changed")
        self.assertEqual(body["channels"]["email"]["reachable"], 4)
        self.assertEqual(body["people"], 4)
        missing = self.send(blast, confirm={"email": 4, "sms": 1})
        self.assertEqual(missing.json()["error"], "count_changed")
        blast.refresh_from_db()
        self.assertEqual(blast.status, "draft")
        self.assertFalse(BlastDelivery.objects.filter(blast=blast).exists())

    def test_daily_caps_answer_409(self):
        blast = self.create_draft()
        SenderMailbox.objects.filter(pk=self.mailbox.pk).update(daily_limit=3)
        response = self.send(blast)
        self.assertEqual((response.status_code, response.json()["error"]), (409, "daily_limit"))
        self.assertEqual(response.json()["limits"], [{"channel": "email", "code": "daily_limit", "remaining_today": 3}])
        SenderMailbox.objects.filter(pk=self.mailbox.pk).update(daily_limit=500)
        with self.settings(CRM_SMS_DAILY_LIMIT=0):
            response = self.send(blast)
        self.assertEqual(response.json()["limits"], [{"channel": "sms", "code": "daily_limit", "remaining_today": 0}])
        self.assertFalse(BlastDelivery.objects.filter(blast=blast).exists())

        # Email still waiting on a live blast counts against the mailbox.
        self.assertEqual(self.send(blast).status_code, 202)
        [mailbox] = self.client.get(PREFIX + "mailboxes/").json()["results"]
        self.assertEqual((mailbox["sent_last_24h"], mailbox["remaining_today"]), (4, 496))

    def test_a_disabled_channel_answers_403(self):
        blast = self.create_draft()
        with self.settings(CRM_SMS_ENABLED=False):
            response = self.send(blast)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {"error": "channel_disabled", "outcome": "channel_disabled", "channel": "sms", "reason": "disabled"},
        )
        with self.settings(LISTMONK_API_TOKEN=""):
            response = self.send(blast)
        self.assertEqual((response.json()["channel"], response.json()["reason"]), ("email", "not_configured"))

    def test_incomplete_drafts_are_refused(self):
        cases = [
            ({"email": {"mailbox_id": str(self.mailbox.pk), "subject": "", "body_markdown": "Hi"}}, "subject_required"),
            ({"sms": {"body": ""}}, "sms_body_required"),
            ({"whatsapp": {"template_id": str(self.template.pk), "variables": {}}}, "template_variable_missing"),
            ({"quick_segment": None}, "audience_required"),
        ]
        for overrides, code in cases:
            with self.subTest(code=code):
                blast = self.create_draft(**overrides)
                response = self.send(blast, key=f"key-{code}")
                self.assertEqual((response.status_code, response.json()["error"]), (400, code))
        MessageTemplate.objects.filter(pk=self.template.pk).update(enabled=False)
        blast = self.create_draft()
        self.assertEqual(self.send(blast, key="key-template").json()["error"], "template_unavailable")
        SenderMailbox.objects.filter(pk=self.mailbox.pk).update(enabled=False)
        self.assertEqual(self.send(blast, key="key-mailbox").json()["error"], "mailbox_disabled")

    def test_marketing_blasts_use_a_saved_segment_and_record_the_consent_basis(self):
        Contact.objects.filter(pk=self.ada.pk).update(marketing_consent="granted")
        Contact.objects.filter(pk=self.bob.pk).update(last_attended_at=self.now - timedelta(days=30))
        segment = self.client.post(
            PREFIX + "segments/",
            {"name": "Everyone", "definition": {"rules": [{"type": "source", "values": ["luma"]}]}},
            format="json",
        ).json()
        blast = self.create_draft(
            purpose="marketing", event_id=None, quick_segment=None, segment_id=segment["id"],
            channels=["email"], sms={"body": ""}, whatsapp={"template_id": None, "variables": {}},
            email={"mailbox_id": str(self.mailbox.pk), "subject": "Our next meetup", "body_markdown": "Hi {{ first_name }}"},
        )
        body = self.client.post(PREFIX + f"blasts/{blast.pk}/audience/").json()
        self.assertEqual(body["channels"]["email"]["reachable"], 2)
        self.assertEqual(body["channels"]["email"]["excluded"]["no_consent"], 3)
        self.assertEqual(body["channels"]["sms"]["reachable"], 0)
        self.assertEqual(self.send(blast, confirm={"email": 2}).status_code, 202)
        self.assertEqual(
            dict(BlastDelivery.objects.filter(blast=blast).values_list("address", "consent_basis")),
            {"ada@example.com": "explicit", "bob@example.com": "inferred_attendance"},
        )
        sms_marketing = self.client.post(
            PREFIX + "blasts/", self.draft_body(purpose="marketing", event_id=None, quick_segment=None), format="json"
        )
        self.assertEqual(sms_marketing.json()["error"], "channel_not_allowed")


@override_settings(**SENDING)
class BlastDispatchDatabaseTests(BlastFixtures, TestCase):
    def test_email_goes_through_one_listmonk_list_and_campaign(self):
        blast = self.queued()
        outcome = blasts.dispatch_blast(blast.pk)
        blast.refresh_from_db()
        self.assertEqual(outcome["status"], "sending")
        self.assertEqual(blast.listmonk_list_id, 100)
        self.assertEqual(self.fake.lists, {100: f"crm-blast-{blast.pk}"})
        self.assertEqual(self.fake.on_list(100), {"ada@example.com", "bob@example.com", "dan@example.com", "eve@example.com"})
        campaign = self.fake.campaigns[blast.listmonk_campaign_id]
        self.assertEqual(campaign["status"], "running")
        self.assertEqual(campaign["from_email"], "Sam from MLAI <sam@mlai.au>")
        self.assertEqual(campaign["lists"], [100])
        self.assertEqual((campaign["content_type"], campaign["type"], campaign["template_id"]), ("markdown", "regular", 3))
        self.assertEqual(campaign["tags"], ["crm", f"blast:{blast.pk}"])
        self.assertEqual(campaign["headers"], [{"Reply-To": "hello@mlai.au"}])
        self.assertEqual(campaign["subject"], "Doors open at 6, {{ .Subscriber.FirstName }}")
        self.assertEqual(
            campaign["body"],
            "Hi {{ .Subscriber.FirstName }}, see you at MLAI Meetup. [Details](https://luma.com/evt-blast@TrackLink)",
        )
        self.assertEqual(self.statuses(blast, "email"), dict.fromkeys(
            ["ada@example.com", "bob@example.com", "dan@example.com", "eve@example.com"], "submitting"
        ))
        self.assertEqual(self.fake.subscriber("ada@example.com")["name"], "Ada")
        self.assertEqual(Contact.objects.get(pk=self.ada.pk).listmonk_subscriber_id, self.fake.subscriber("ada@example.com")["id"])
        # SMS and WhatsApp went straight to Twilio.
        self.assertEqual(sorted(message["To"] for message in self.fake.twilio), ["+61412345672", "whatsapp:+61412345671"])
        self.assertEqual(self.statuses(blast, "sms"), {"bob@example.com": "sent"})

        self.finish_campaign(blast)
        self.assertEqual(blasts.dispatch_blast(blast.pk)["status"], "sent")
        blast.refresh_from_db()
        self.assertIsNotNone(blast.sent_at)
        self.assertEqual(set(self.statuses(blast, "email").values()), {"sent"})
        self.assertEqual(blast.stats["email"]["listmonk_status"], "finished")
        self.assertEqual(self.fake.count("POST", r"/api/lists"), 1)
        self.assertEqual(self.fake.count("POST", r"/api/campaigns"), 1)
        self.assertEqual(self.fake.count("POST", r"/api/subscribers"), 4)

    def test_a_retried_dispatch_never_makes_a_second_list_or_campaign(self):
        blast = self.queued(channels=["email"], sms={"body": ""}, whatsapp={"template_id": None, "variables": {}})
        self.fake.fail_next("POST", r"/api/subscribers")
        self.fake.fail_next("PUT", r"/api/campaigns/\d+/status")
        first = blasts.dispatch_blast(blast.pk, twilio_budget=0)
        self.assertEqual((first["status"], first["deferred"]), ("sending", "http_503"))
        self.assertEqual(set(self.statuses(blast, "email").values()), {"queued"})
        second = blasts.dispatch_blast(blast.pk)
        self.assertEqual(second["deferred"], "http_503")
        blast.refresh_from_db()
        self.assertIsNotNone(blast.listmonk_campaign_id)
        self.assertEqual(self.fake.campaigns[blast.listmonk_campaign_id]["status"], "draft")
        third = blasts.dispatch_blast(blast.pk)
        self.assertEqual(third["email"]["campaign"], "running")
        self.assertEqual(self.fake.count("POST", r"/api/lists"), 1)
        self.assertEqual(self.fake.count("POST", r"/api/campaigns"), 1)
        self.assertEqual(len(self.fake.subscribers), 4)

    def test_ids_lost_after_a_create_are_found_by_name(self):
        blast = self.queued(channels=["email"], sms={"body": ""}, whatsapp={"template_id": None, "variables": {}})
        # listmonk created both, but the dispatcher stopped before storing the ids.
        self.fake.lists[50] = f"crm-blast-{blast.pk}"
        self.fake.campaigns[60] = {"id": 60, "name": f"crm-blast-{blast.pk}", "status": "draft"}
        self.fake.add_subscriber("eve@example.com", "Eve", lists=[50])
        blasts.dispatch_blast(blast.pk)
        blast.refresh_from_db()
        self.assertEqual((blast.listmonk_list_id, blast.listmonk_campaign_id), (50, 60))
        self.assertEqual(self.fake.count("POST", r"/api/lists"), 0)
        self.assertEqual(self.fake.count("POST", r"/api/campaigns"), 0)
        self.assertEqual(self.fake.campaigns[60]["status"], "running")
        self.assertEqual(self.fake.campaigns[60]["lists"], [50])
        self.assertEqual(self.fake.count("POST", r"/api/subscribers"), 3)

    def test_opt_outs_since_queueing_are_dropped_at_dispatch(self):
        blast = self.queued(channels=["email"], sms={"body": ""}, whatsapp={"template_id": None, "variables": {}})
        Contact.objects.filter(pk=self.bob.pk).update(email_status="unsubscribed")
        self.fake.add_subscriber("dan@example.com", "Dan", status="blocklisted")
        blasts.dispatch_blast(blast.pk)
        codes = dict(BlastDelivery.objects.filter(blast=blast).values_list("address", "error_code"))
        self.assertEqual(codes["bob@example.com"], "opted_out")
        self.assertEqual(codes["dan@example.com"], "listmonk_opted_out")
        self.assertEqual(self.statuses(blast, "email")["bob@example.com"], "unsubscribed")
        blast.refresh_from_db()
        self.assertEqual(self.fake.on_list(blast.listmonk_list_id), {"ada@example.com", "eve@example.com"})

    def test_status_rolls_up_partially_sent_and_failed(self):
        blast = self.queued(
            confirm=self.confirm(sms=2),
            channels=["email", "sms"],
            whatsapp={"template_id": None, "variables": {}},
            sms_only_without_whatsapp=False,
        )
        with self.settings(CRM_SMS_ENABLED=False):
            self.assertEqual(blasts.dispatch_blast(blast.pk)["status"], "sending")
            self.finish_campaign(blast)
            self.assertEqual(blasts.dispatch_blast(blast.pk)["status"], "partially_sent")
        blast.refresh_from_db()
        self.assertEqual(blast.last_error, "sms blocked")
        self.assertIsNotNone(blast.sent_at)
        self.assertEqual(set(self.statuses(blast, "sms").values()), {"queued"})
        self.assertEqual(self.fake.twilio, [])

        refused = self.queued(
            key="key-refused", channels=["email"], sms={"body": ""}, whatsapp={"template_id": None, "variables": {}},
        )
        self.fake.fail_next("POST", r"/api/campaigns", status=400)
        self.assertEqual(blasts.dispatch_blast(refused.pk)["status"], "failed")
        self.assertEqual(set(self.statuses(refused, "email").values()), {"failed"})

    def test_the_runner_claims_live_blasts(self):
        blast = self.queued()
        result = blasts.run_blast_dispatch_scheduler()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["outcomes"], {str(blast.pk): "sending"})
        self.assertEqual(blasts.run_blast_dispatch_scheduler()["reason"], "throttled")
        with self.settings(CRM_SENDING_ENABLED=False, CRM_SMS_ENABLED=False, CRM_WHATSAPP_ENABLED=False):
            self.assertEqual(blasts.run_blast_dispatch_scheduler()["reason"], "disabled")


@override_settings(**SENDING)
class BlastCancelDatabaseTests(BlastFixtures, TestCase):
    def test_cancel_before_dispatch_sends_nothing(self):
        blast = self.queued()
        response = self.client.post(PREFIX + f"blasts/{blast.pk}/cancel/")
        self.assertEqual((response.status_code, response.json()["status"]), (200, "cancelled"))
        self.assertEqual(blasts.dispatch_blast(blast.pk)["status"], "cancelled")
        self.assertEqual((self.fake.calls, self.fake.twilio), ([], []))
        audit = CrmAuditEvent.objects.get(action="send", target_id=str(blast.pk), detail__operation="cancel")
        self.assertEqual((audit.detail["previous_status"], audit.actor), ("queued", self.admin))
        again = self.client.post(PREFIX + f"blasts/{blast.pk}/cancel/")
        self.assertEqual(again.status_code, 200)

    def test_cancel_stops_a_running_campaign(self):
        blast = self.queued(channels=["email"], sms={"body": ""}, whatsapp={"template_id": None, "variables": {}})
        blasts.dispatch_blast(blast.pk)
        response = self.client.post(PREFIX + f"blasts/{blast.pk}/cancel/")
        self.assertEqual(response.json()["status"], "cancelled")
        blast.refresh_from_db()
        self.assertEqual(self.fake.campaigns[blast.listmonk_campaign_id]["status"], "cancelled")
        self.assertEqual(set(self.statuses(blast, "email").values()), {"unknown"})
        self.assertEqual(blast.stats["email"]["listmonk_status"], "cancelled")

    def test_a_cancel_listmonk_missed_is_retried_by_the_sync(self):
        blast = self.queued(channels=["email"], sms={"body": ""}, whatsapp={"template_id": None, "variables": {}})
        blasts.dispatch_blast(blast.pk)
        self.fake.fail_next("PUT", r"/api/campaigns/\d+/status")
        self.client.post(PREFIX + f"blasts/{blast.pk}/cancel/")
        blast.refresh_from_db()
        self.assertEqual((blast.status, blast.last_error), ("cancelled", "listmonk_cancel_pending"))
        self.assertTrue(blast.stats["email"]["cancel_pending"])
        listmonk_sync.sync_listmonk(now=timezone.now())
        blast.refresh_from_db()
        self.assertEqual(self.fake.campaigns[blast.listmonk_campaign_id]["status"], "cancelled")
        self.assertFalse(blast.stats["email"]["cancel_pending"])
        self.assertEqual(blast.last_error, "")

    def test_twilio_sends_stop_once_cancelled(self):
        blast = self.create_draft(
            channels=["sms"], sms_only_without_whatsapp=False,
            email={"mailbox_id": None, "subject": "", "body_markdown": ""},
        )
        self.assertEqual(self.send(blast, confirm={"sms": 2}).status_code, 202)
        blasts.cancel_blast(blast.pk, actor=self.admin)
        blasts.dispatch_blast(blast.pk)
        self.assertEqual(self.fake.twilio, [])
        self.assertEqual(set(self.statuses(blast, "sms").values()), {"queued"})

    def test_finished_blasts_cannot_be_cancelled(self):
        blast = self.queued()
        Blast.objects.filter(pk=blast.pk).update(status="sent")
        response = self.client.post(PREFIX + f"blasts/{blast.pk}/cancel/")
        self.assertEqual((response.status_code, response.json()["error"]), (409, "blast_not_cancellable"))


@override_settings(**SENDING)
class ListmonkSyncDatabaseTests(BlastFixtures, TestCase):
    def sent_campaign(self):
        blast = self.queued()
        blasts.dispatch_blast(blast.pk)
        blast.refresh_from_db()
        return blast, blast.listmonk_campaign_id

    def test_views_clicks_bounces_and_unsubscribes_reach_each_delivery(self):
        blast, campaign_id = self.sent_campaign()
        self.finish_campaign(blast)
        self.fake.views[campaign_id] |= {"ada@example.com", "bob@example.com"}
        self.fake.link_clicks[(campaign_id, "https://luma.com/evt-blast")] |= {"ada@example.com"}
        self.fake.campaign_bounces[campaign_id] |= {"dan@example.com"}
        self.fake.unsubscribe("eve@example.com", blast.listmonk_list_id)
        now = timezone.now()

        summary = listmonk_sync.sync_listmonk(now=now)

        rows = {
            row.address: row
            for row in BlastDelivery.objects.filter(blast=blast, channel="email")
        }
        self.assertEqual(rows["ada@example.com"].first_opened_at, now)
        self.assertEqual(rows["ada@example.com"].first_clicked_at, now)
        self.assertEqual(rows["ada@example.com"].clicked_urls, ["https://luma.com/evt-blast"])
        self.assertEqual(rows["bob@example.com"].first_opened_at, now)
        self.assertIsNone(rows["bob@example.com"].first_clicked_at)
        self.assertEqual(rows["dan@example.com"].status, "bounced")
        self.assertEqual((rows["eve@example.com"].status, rows["eve@example.com"].error_code), ("unsubscribed", ""))
        eve = Contact.objects.get(pk=self.eve.pk)
        self.assertEqual((eve.email_status, eve.marketing_consent), ("unsubscribed", "withdrawn"))
        event = ContactConsentEvent.objects.get(contact=eve)
        self.assertEqual((event.channel, event.kind, event.source), ("email", "unsubscribed", "listmonk:unsubscribe"))
        self.assertEqual(self.fake.subscriber("eve@example.com")["status"], "blocklisted")
        blast.refresh_from_db()
        self.assertEqual(blast.status, "sent")
        self.assertEqual(blast.stats["email"]["views"], 2)
        self.assertEqual((summary["opened"], summary["clicked"], summary["bounced"], summary["unsubscribed"]), (2, 1, 1, 1))
        self.assertTrue(CrmAuditEvent.objects.filter(action="sync", target_type="listmonk").exists())

        detail = self.client.get(PREFIX + f"blasts/{blast.pk}/").json()
        self.assertEqual(
            detail["per_channel"]["email"],
            {"recipients": 4, "sent": 4, "opened": 2, "clicked": 1, "bounced": 1, "unsubscribed": 1},
        )
        openers = self.client.post(
            PREFIX + "segments/preview/",
            {"definition": {"rules": [{"type": "blast_engagement", "blast_id": str(blast.pk), "action": "opened"}]}},
            format="json",
        ).json()
        self.assertEqual(openers["count"], 2)

        # A second pass changes nothing and adds no consent events.
        listmonk_sync.sync_listmonk(now=now + timedelta(minutes=5))
        self.assertEqual(ContactConsentEvent.objects.filter(contact=eve).count(), 1)
        self.assertEqual(BlastDelivery.objects.get(pk=rows["ada@example.com"].pk).first_opened_at, now)

    def test_blocklisted_subscribers_become_bounces_complaints_or_unsubscribes(self):
        Contact.objects.filter(pk=self.ada.pk).update(marketing_consent="granted")
        Contact.objects.filter(pk=self.dan.pk).update(email_status="complained")
        for contact in (self.ada, self.bob, self.dan, self.eve):
            self.fake.add_subscriber(contact.email, status="blocklisted")
        self.fake.bounced |= {"ada@example.com"}
        self.fake.complaints |= {"bob@example.com"}
        self.fake.add_subscriber("fay@example.com", lists=[])
        listmonk_sync.sync_listmonk(now=timezone.now())

        ada, bob, dan, eve = (Contact.objects.get(pk=item.pk) for item in (self.ada, self.bob, self.dan, self.eve))
        self.assertEqual((ada.email_status, ada.marketing_consent), ("bounced", "granted"))
        self.assertEqual((bob.email_status, bob.marketing_consent), ("complained", "withdrawn"))
        self.assertEqual(dan.email_status, "complained")
        self.assertEqual((eve.email_status, eve.marketing_consent), ("unsubscribed", "withdrawn"))
        self.assertEqual(
            set(ContactConsentEvent.objects.values_list("contact__email", "kind", "source")),
            {
                ("ada@example.com", "bounced", "listmonk:bounce"),
                ("bob@example.com", "complained", "listmonk:complaint"),
                ("dan@example.com", "unsubscribed", "listmonk:blocklist"),
                ("eve@example.com", "unsubscribed", "listmonk:blocklist"),
            },
        )
        self.assertEqual(Contact.objects.get(pk=self.fay.pk).email_status, "active")

    def test_opt_outs_made_in_chat_are_blocklisted_in_listmonk(self):
        subscriber = self.fake.add_subscriber("bob@example.com")
        Contact.objects.filter(pk=self.bob.pk).update(listmonk_subscriber_id=subscriber["id"])
        response = self.client.post(
            PREFIX + f"contacts/{self.bob.pk}/stop-messaging/", {"channels": ["email"]}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        listmonk_sync.sync_listmonk(now=timezone.now())
        self.assertEqual(self.fake.subscriber("bob@example.com")["status"], "blocklisted")

    def test_the_runner_is_throttled_and_defers_outages(self):
        self.fake.fail_next("GET", r"/api/subscribers")
        self.assertEqual(listmonk_sync.run_listmonk_sync_scheduler()["status"], "deferred")
        self.assertEqual(listmonk_sync.run_listmonk_sync_scheduler()["reason"], "throttled")


@override_settings(**SENDING)
class BlastApiDatabaseTests(BlastFixtures, TestCase):
    def test_members_are_refused_everywhere(self):
        blast = self.create_draft()
        requests = [
            ("get", "mailboxes/"),
            ("get", "whatsapp-templates/"),
            ("get", "blasts/"),
            ("post", "blasts/"),
            ("get", f"blasts/{blast.pk}/"),
            ("patch", f"blasts/{blast.pk}/"),
            ("post", f"blasts/{blast.pk}/audience/"),
            ("post", f"blasts/{blast.pk}/test/"),
            ("post", f"blasts/{blast.pk}/send/"),
            ("post", f"blasts/{blast.pk}/cancel/"),
            ("get", f"blasts/{blast.pk}/deliveries/"),
            ("get", f"blasts/{blast.pk}/replies/"),
        ]
        for method, path in requests:
            with self.subTest(method=method, path=path):
                response = getattr(self.member_client, method)(PREFIX + path, {}, format="json")
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["error"], "not_authorised")
        with self.settings(COMMUNITY_CHAT_CRM_ENABLED=False):
            self.assertEqual(self.client.get(PREFIX + "blasts/").status_code, 404)
        self.assertEqual(Blast.objects.get(pk=blast.pk).status, "draft")
        self.assertEqual(self.fake.calls, [])

    def test_drafts_list_and_detail(self):
        blast = self.create_draft()
        detail = self.client.get(PREFIX + f"blasts/{blast.pk}/").json()
        self.assertEqual(detail["status"], "draft")
        self.assertEqual(detail["audience"], {"kind": "quick_segment", "key": "going", "label": "Going · MLAI Meetup"})
        self.assertEqual(detail["email"]["mailbox_id"], str(self.mailbox.pk))
        self.assertEqual(detail["whatsapp"], {"template_id": str(self.template.pk), "variables": {"3": "Doors open at 6"}})
        self.assertEqual(detail["created_by"], {"display_name": self.admin.full_name})

        patched = self.client.patch(
            PREFIX + f"blasts/{blast.pk}/", {"email": {"subject": "New subject"}, "channels": ["email"]}, format="json"
        ).json()
        self.assertEqual((patched["title"], patched["channels"]), ("New subject", ["email"]))
        self.assertEqual(patched["email"]["body_markdown"], self.draft_body()["email"]["body_markdown"])
        bad_tag = self.client.patch(PREFIX + f"blasts/{blast.pk}/", {"sms": {"body": "Hi {{ email }}"}}, format="json")
        self.assertEqual(bad_tag.json(), {"error": "unsupported_merge_tag", "outcome": "unsupported_merge_tag", "tags": ["email"], "field": "sms_body"})
        missing = self.client.patch(PREFIX + f"blasts/{blast.pk}/", {"email": {"mailbox_id": str(uuid.uuid4())}}, format="json")
        self.assertEqual((missing.status_code, missing.json()["error"]), (404, "mailbox_not_found"))
        no_purpose = self.client.post(PREFIX + "blasts/", {"channels": ["email"]}, format="json")
        self.assertEqual(no_purpose.json()["fields"], {"purpose": ["required"]})

        self.queued(key="key-other", channels=["email"], sms={"body": ""}, whatsapp={"template_id": None, "variables": {}})
        listed = self.client.get(PREFIX + "blasts/?status=draft").json()["results"]
        self.assertEqual([row["id"] for row in listed], [str(blast.pk)])
        self.assertEqual(len(self.client.get(PREFIX + f"blasts/?event_id={self.event.pk}").json()["results"]), 2)
        self.assertEqual(len(self.client.get(PREFIX + "blasts/?channel=whatsapp").json()["results"]), 0)
        self.assertEqual(self.client.get(PREFIX + "blasts/?channel=fax").json()["error"], "invalid_filter")
        audits = CrmAuditEvent.objects.filter(action="edit", target_type="blast", target_id=str(blast.pk))
        self.assertEqual(sorted(audit.detail["operation"] for audit in audits), ["create", "update"])

    def test_deliveries_are_masked_and_replies_listed(self):
        blast = self.queued()
        InboundMessage.objects.create(
            contact=self.bob, channel="sms", from_address="+61412345672", body="See you there",
            provider_id="SM-reply", blast=blast, received_at=timezone.now(),
        )
        body = self.client.get(PREFIX + f"blasts/{blast.pk}/deliveries/?channel=email").json()
        self.assertEqual(body["count"], 4)
        self.assertIn("a•••@example.com", {row["address"] for row in body["results"]})
        sms = self.client.get(PREFIX + f"blasts/{blast.pk}/deliveries/?channel=sms").json()["results"]
        self.assertEqual([row["address"] for row in sms], ["+61•••672"])
        self.assertEqual(self.client.get(PREFIX + f"blasts/{blast.pk}/deliveries/?status=sent").json()["count"], 0)
        replies = self.client.get(PREFIX + f"blasts/{blast.pk}/replies/").json()["results"]
        self.assertEqual(replies, [{
            "contact_id": str(self.bob.pk), "display_name": "Bob", "channel": "sms",
            "body": "See you there", "received_at": replies[0]["received_at"],
        }])
        self.assertEqual(self.client.get(PREFIX + f"blasts/{blast.pk}/").json()["replies"], 1)

    def test_mailboxes_and_templates(self):
        MessageTemplate.objects.create(name="draft_template", content_sid="HX2", approved=False, enabled=True)
        mailboxes = self.client.get(PREFIX + "mailboxes/").json()["results"]
        self.assertEqual(mailboxes[0]["email"], "sam@mlai.au")
        self.assertEqual((mailboxes[0]["sent_last_24h"], mailboxes[0]["remaining_today"]), (0, 500))
        templates = self.client.get(PREFIX + "whatsapp-templates/").json()["results"]
        self.assertEqual([item["name"] for item in templates], ["mlai_event_reminder"])
        self.assertEqual(templates[0]["variables"][2], {"index": 3, "source": "custom", "label": "Details"})

    def test_tests_go_only_to_the_callers_own_email_and_phone(self):
        type(self.admin).objects.filter(pk=self.admin.pk).update(phone="0412 345 999")
        blast = self.create_draft()
        response = self.client.post(
            PREFIX + f"blasts/{blast.pk}/test/", {"channels": ["email", "sms", "whatsapp"]}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json(),
            {
                "sent": [
                    {"channel": "email", "to": "a•••@example.test"},
                    {"channel": "sms", "to": "+61•••999"},
                    {"channel": "whatsapp", "to": "+61•••999"},
                ],
                "failed": [],
            },
        )
        [(campaign_id, payload)] = self.fake.tests
        self.assertEqual(payload["subscribers"], ["admin@example.test"])
        self.assertEqual(self.fake.campaigns[campaign_id]["status"], "draft")
        self.assertEqual([message["To"] for message in self.fake.twilio], ["+61412345999", "whatsapp:+61412345999"])
        self.assertNotIn("StatusCallback", self.fake.twilio[0])
        self.assertFalse(BlastDelivery.objects.filter(blast=blast).exists())
        self.assertEqual(Blast.objects.get(pk=blast.pk).status, "draft")
        audit = CrmAuditEvent.objects.get(action="send", target_id=str(blast.pk))
        self.assertEqual(audit.detail, {"operation": "test", "channels": ["email", "sms", "whatsapp"], "sent": 3})

        # A second test reuses the draft list and campaign.
        self.client.post(PREFIX + f"blasts/{blast.pk}/test/", {"channels": ["email"]}, format="json")
        self.assertEqual((self.fake.count("POST", r"/api/lists"), self.fake.count("POST", r"/api/campaigns")), (1, 1))

        type(self.admin).objects.filter(pk=self.admin.pk).update(phone="")
        no_phone = self.client.post(PREFIX + f"blasts/{blast.pk}/test/", {"channels": ["sms"]}, format="json")
        self.assertEqual((no_phone.status_code, no_phone.json()["error"]), (400, "test_phone_missing"))
        with self.settings(LISTMONK_BASE_URL=""):
            unconfigured = self.client.post(PREFIX + f"blasts/{blast.pk}/test/", {"channels": ["email"]}, format="json")
        self.assertEqual((unconfigured.status_code, unconfigured.json()["error"]), (409, "channel_disabled"))
        not_chosen = self.client.patch(PREFIX + f"blasts/{blast.pk}/", {"channels": ["email"]}, format="json")
        self.assertEqual(not_chosen.status_code, 200)
        self.assertEqual(
            self.client.post(PREFIX + f"blasts/{blast.pk}/test/", {"channels": ["sms"]}, format="json").json()["error"],
            "channel_not_selected",
        )
