"""PostgreSQL-backed checks for Admin → Sending: domain health and sender edits; runs in CI.

DNS never leaves the process: ``crm.sending_domains.lookup`` is mocked.
"""

import uuid
from unittest.mock import patch

from django.test import TestCase, override_settings

from crm.models import CrmAuditEvent, SenderMailbox

from .api_fixtures import PREFIX, CrmApiMixin, crm_enabled
from .test_sending_domains_unit import resend_zone, zone_lookup

DOMAINS = override_settings(CRM_SENDING_DOMAINS=["mlai.au:resend", "mlai-aus.com:resend"])


@crm_enabled
@DOMAINS
class SendingApiDatabaseTests(CrmApiMixin, TestCase):
    def setUp(self):
        super().setUp()
        zone = {**resend_zone("mlai.au"), **resend_zone("mlai-aus.com")}
        zone[("resend._domainkey.mlai-aus.com", "TXT")] = []
        self.lookup = self.enterContext(patch("crm.sending_domains.lookup", side_effect=zone_lookup(zone)))

    def create(self, **fields):
        body = {"email": "hey@mlai.au", "display_name": "MLAI", **fields}
        return self.client.post(PREFIX + "mailboxes/", body, format="json")

    def test_members_are_refused(self):
        some_id = uuid.uuid4()
        for method, path in [
            ("get", "sending-domains/"),
            ("post", "mailboxes/"),
            ("get", f"mailboxes/{some_id}/"),
            ("patch", f"mailboxes/{some_id}/"),
        ]:
            with self.subTest(method=method, path=path):
                response = getattr(self.member_client, method)(PREFIX + path, {}, format="json")
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.lookup.call_count, 0)

    def test_domains_list_their_checks_and_senders(self):
        SenderMailbox.objects.create(
            email="old@legacy.example", display_name="Old", domain="legacy.example", route="resend"
        )
        self.create(reply_to="hi@mlai.au")

        results = self.client.get(PREFIX + "sending-domains/").json()["results"]

        self.assertEqual(
            [(item["domain"], item["configured"], item["status"], item["mailbox_count"]) for item in results],
            [
                ("mlai.au", True, "verified", 1),
                ("mlai-aus.com", True, "failing", 0),
                ("legacy.example", False, "not_configured", 1),
            ],
        )
        self.assertEqual(results[0]["route"], "resend")
        self.assertTrue(results[0]["checked_at"].endswith("Z"))
        self.assertEqual(results[0]["records"][0]["key"], "dkim")
        self.assertEqual(results[2]["records"], [])

    def test_refresh_skips_the_cache(self):
        self.client.get(PREFIX + "sending-domains/")
        calls = self.lookup.call_count
        self.client.get(PREFIX + "sending-domains/")
        self.assertEqual(self.lookup.call_count, calls)
        self.client.get(PREFIX + "sending-domains/?refresh=1")
        self.assertEqual(self.lookup.call_count, calls * 2)

    def test_a_sender_is_added_on_a_listed_domain_with_its_route(self):
        response = self.create(email="Hey@MLAI.au", reply_to="hi@mlai.au", daily_limit=50, enabled=True)

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(
            {key: body[key] for key in ("email", "display_name", "domain", "route", "reply_to", "daily_limit", "enabled")},
            {
                "email": "hey@mlai.au",
                "display_name": "MLAI",
                "domain": "mlai.au",
                "route": "resend",
                "reply_to": "hi@mlai.au",
                "daily_limit": 50,
                "enabled": True,
            },
        )
        self.assertEqual((body["sent_last_24h"], body["remaining_today"]), (0, 50))
        event = CrmAuditEvent.objects.get(target_type="mailbox")
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(event.target_id, body["id"])
        self.assertEqual(
            event.detail,
            {"operation": "create", "fields": ["daily_limit", "display_name", "email", "enabled", "reply_to"]},
        )
        self.assertEqual(self.client.get(PREFIX + f"mailboxes/{body['id']}/").json()["email"], "hey@mlai.au")

    def test_unlisted_and_duplicate_addresses_are_refused(self):
        unlisted = self.create(email="hey@gmail.com")
        self.assertEqual(
            (unlisted.status_code, unlisted.json()),
            (400, {"error": "sending_domain_not_configured", "outcome": "sending_domain_not_configured", "domain": "gmail.com"}),
        )
        self.create()
        duplicate = self.create(email="HEY@mlai.au")
        self.assertEqual((duplicate.status_code, duplicate.json()["error"]), (409, "mailbox_exists"))
        self.assertEqual(SenderMailbox.objects.count(), 1)

    def test_a_sender_switches_on_only_while_its_domain_verifies(self):
        failing = self.create(email="hey@mlai-aus.com", enabled=True)
        self.assertEqual((failing.status_code, failing.json()["error"]), (400, "sending_domain_unverified"))
        self.assertFalse(SenderMailbox.objects.exists())

        mailbox_id = self.create(email="hey@mlai-aus.com").json()["id"]
        refused = self.client.patch(PREFIX + f"mailboxes/{mailbox_id}/", {"enabled": True}, format="json")
        self.assertEqual(refused.json()["error"], "sending_domain_unverified")
        self.assertFalse(SenderMailbox.objects.get(pk=mailbox_id).enabled)

    def test_edits_change_only_sent_fields_and_the_address_is_fixed(self):
        mailbox_id = self.create(reply_to="hi@mlai.au", enabled=True).json()["id"]

        edited = self.client.patch(
            PREFIX + f"mailboxes/{mailbox_id}/", {"display_name": "Team MLAI", "reply_to": ""}, format="json"
        )
        moved = self.client.patch(PREFIX + f"mailboxes/{mailbox_id}/", {"email": "x@mlai.au"}, format="json")
        missing = self.client.patch(PREFIX + f"mailboxes/{uuid.uuid4()}/", {"enabled": False}, format="json")

        self.assertEqual(edited.status_code, 200)
        mailbox = SenderMailbox.objects.get(pk=mailbox_id)
        self.assertEqual((mailbox.display_name, mailbox.reply_to, mailbox.enabled), ("Team MLAI", "", True))
        self.assertEqual((moved.status_code, moved.json()["fields"]), (400, {"email": ["unknown_field"]}))
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(
            CrmAuditEvent.objects.filter(target_type="mailbox").latest("created_at").detail,
            {"operation": "update", "fields": ["display_name", "reply_to"]},
        )

    def test_a_sender_on_an_unlisted_domain_can_still_be_switched_off(self):
        mailbox = SenderMailbox.objects.create(
            email="old@legacy.example", display_name="Old", domain="legacy.example", route="resend", enabled=True
        )
        off = self.client.patch(PREFIX + f"mailboxes/{mailbox.pk}/", {"enabled": False}, format="json")
        on = self.client.patch(PREFIX + f"mailboxes/{mailbox.pk}/", {"enabled": True}, format="json")
        self.assertEqual((off.status_code, off.json()["enabled"]), (200, False))
        self.assertEqual(on.json()["error"], "sending_domain_unverified")
