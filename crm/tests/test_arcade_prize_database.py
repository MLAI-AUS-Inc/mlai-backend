"""Real database stock, CRM permission and private Resend fulfillment regressions."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat.arcade_inventory import CODEX_PREFIX, import_stock, reserve_prize, stock_summary, ticket_payload
from community_chat.crm.arcade_views import ArcadeStockView
from community_chat.crm.errors import CrmError
from community_chat.volunteer.access import community_id
from community_chat.volunteer.models import VolunteerSourceReceipt as Receipt
from crm.arcade_delivery import _deliver, run_arcade_prize_delivery
from crm.models import Contact, CrmAuditEvent
from system_email.models import SystemEmail, SystemEmailSuppression
from system_email.transport_resend import SystemEmailRetryable


@override_settings(COMMUNITY_CHAT_CRM_ENABLED=True, COMMUNITY_CHAT_VOLUNTEER_COMMUNITY="arcade-test",
                   SYSTEM_EMAIL_RESEND_API_KEY="mock-resend-for-tests", MLAI_SYSTEM_FROM_EMAIL="Arcade <arcade@example.org>")
class ArcadePrizeTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user("admin@example.test", is_superuser=True)
        self.winner = get_user_model().objects.create_user("winner@example.test", email_verified_at=timezone.now())

    def stock(self, *codes):
        return import_stock(self.admin, {"prize": "codex", "codes": list(codes)})

    def round(self):
        return Receipt.objects.create(community=community_id(), source_key=f"round-{Receipt.objects.count()}",
                                      origin="mlai_arcade", kind="round", status="ready",
                                      actor=self.winner, occurred_at=timezone.now())

    def test_admin_gate_and_atomic_encrypted_idempotent_paste(self):
        request = APIRequestFactory().post("/crm/arcade-stock/", {"prize": "codex", "codes": ["FAKE-ONLY"]}, format="json")
        force_authenticate(request, self.winner)
        with patch("community_chat.admin_views.is_mlai_admin_request", return_value=False):
            self.assertEqual(ArcadeStockView.as_view()(request).status_code, 403)
        with self.assertRaises(CrmError):
            self.stock("FAKE-ONLY", "https://invalid.example/a")
        self.assertFalse(Receipt.objects.exists())
        summary = self.stock("FAKE-ONLY", CODEX_PREFIX + "FAKE-ONLY", "FAKE-SECOND")
        self.assertEqual((summary["inserted"], summary["duplicates"]), (2, 1))
        self.assertEqual(summary["codex"]["available"], 2)
        self.assertNotIn("FAKE-ONLY", str(summary))
        self.assertNotIn("FAKE-ONLY", str(list(Receipt.objects.values())))
        self.assertNotIn("FAKE-ONLY", str(list(CrmAuditEvent.objects.values())))
        self.assertEqual(self.stock("FAKE-ONLY")["duplicates"], 1)
        with override_settings(COMMUNITY_CHAT_VOLUNTEER_COMMUNITY="another-scope"):
            repeated = self.stock("FAKE-ONLY")
            self.assertEqual(repeated["duplicates"], 1)
            self.assertEqual(repeated["codex"]["available"], 0)

    def test_global_pacing_boundary_and_rolled_back_pull(self):
        self.stock("FAKE-ONE", "FAKE-TWO", "FAKE-THREE")
        first, second = self.round(), self.round()
        now = timezone.now()
        with self.assertRaises(RuntimeError), transaction.atomic():
            self.assertIsNotNone(reserve_prize(first, "codex_credits", now))
            raise RuntimeError("rollback complete spin")
        self.assertEqual(stock_summary()["codex"]["available"], 3)
        self.assertIsNotNone(reserve_prize(first, "codex_credits", now))
        self.assertIsNone(reserve_prize(second, "codex_credits", now + timedelta(hours=36, microseconds=-1)))
        self.assertIsNotNone(reserve_prize(second, "codex_credits", now + timedelta(hours=36)))

    def test_ticket_event_expiry_and_winner_binding(self):
        now = timezone.now()
        offer = {"prize": "ticket", "eventName": "Test event", "eventUrl": "https://luma.com/arcade-example",
                 "expiresAt": (now + timedelta(days=7)).isoformat(), "codes": ["TEST-50"]}
        import_stock(self.admin, offer)
        rnd = self.round()
        self.assertIsNone(reserve_prize(rnd, "event_ticket", now + timedelta(days=7)))
        self.assertIsNotNone(reserve_prize(rnd, "event_ticket", now))
        self.assertEqual(ticket_payload(rnd), {"eventName": "Test event", "eventUrl": offer["eventUrl"], "ticketCode": "TEST-50"})
        self.assertEqual(ticket_payload(SimpleNamespace(community=rnd.community, pk=rnd.pk, actor_id=self.admin.pk)), {})
        self.assertIsNone(reserve_prize(self.round(), "event_ticket", now))

    def test_cannot_award_when_either_sender_or_recipient_is_unavailable(self):
        self.stock("FAKE-ONLY")
        rnd = self.round()
        with override_settings(SYSTEM_EMAIL_RESEND_API_KEY=""):
            self.assertIsNone(reserve_prize(rnd, "codex_credits", timezone.now()))
        with patch.object(rnd.actor, "email_verified_at", None):
            self.assertIsNone(reserve_prize(rnd, "codex_credits", timezone.now()))
        contact = Contact.objects.create(email=self.winner.email, email_status="bounced")
        self.assertIsNone(reserve_prize(rnd, "codex_credits", timezone.now()))
        contact.email_status = "active"
        contact.save(update_fields=("email_status",))
        SystemEmailSuppression.objects.create(email=self.winner.email)
        self.assertIsNone(reserve_prize(rnd, "codex_credits", timezone.now()))
        self.assertEqual(stock_summary()["codex"]["available"], 1)

    @patch("system_email.transport_resend.http_client.post")
    def test_private_multipart_resend_email_and_scheduler_replay(self, post):
        self.stock("SYNTHETIC-PRIZE")
        awarded = reserve_prize(self.round(), "codex_credits", timezone.now())
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = {"id": "mock-provider-receipt"}
        self.assertEqual(run_arcade_prize_delivery()["processed"], 1)
        self.assertEqual(run_arcade_prize_delivery()["processed"], 0)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["to"], [self.winner.email])
        for part in [payload["html"], payload["text"]]:
            self.assertIn("SYNTHETIC-PRIZE", part)
            self.assertIn(CODEX_PREFIX + "SYNTHETIC-PRIZE", part)
            self.assertIn("same", part)
        self.assertNotIn("SYNTHETIC", payload["subject"])
        self.assertNotIn("SYNTHETIC", str(list(SystemEmail.objects.values())) + str(list(CrmAuditEvent.objects.values())))
        self.assertEqual(Receipt.objects.get(pk=awarded["stockId"]).status, "sent")
        self.assertEqual(stock_summary()["delivery"]["sent"], 1)

    @patch("system_email.transport_resend.send", side_effect=SystemEmailRetryable("timeout"))
    def test_ambiguous_retry_reuses_key_and_stops_inside_resend_window(self, send):
        self.stock("SYNTHETIC-PRIZE")
        now = timezone.now()
        stock_id = reserve_prize(self.round(), "codex_credits", now)["stockId"]
        _deliver(stock_id, now)
        _deliver(stock_id, now)
        self.assertEqual(send.call_count, 1)
        _deliver(stock_id, now + timedelta(minutes=3))
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0], send.call_args_list[1])
        _deliver(stock_id, now + timedelta(hours=23))
        self.assertEqual(send.call_count, 2)
        self.assertEqual(stock_summary()["delivery"]["needsReview"], 1)
        self.assertEqual(stock_summary()["codex"]["available"], 0)
