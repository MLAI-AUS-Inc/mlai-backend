"""Synthetic publication, ladder, member-isolation and wallet replay regressions."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from community_chat import arcade
from community_chat.volunteer.credits import record_wallet_credit
from community_chat.volunteer.services import contribution_total
from organizations.models import Organization
from roo.models import Ledger, PointsAccount
from roo.services import PointsService
from startup_updates.models import MonthlyUpdateDraft
from startup_updates.rewards import award_completion

API = "/api/v1/community-chat/arcade/rounds/"
FLAGS = dict(COMMUNITY_CHAT_VOLUNTEER_COMMUNITY="arcade-tests",
             COMMUNITY_CHAT_VOLUNTEER_ONE_RECORD_ENABLED=True,
             COMMUNITY_CHAT_VOLUNTEER_AWARDS_ENABLED=False)


class ArcadeOddsTests(SimpleTestCase):
    def test_cutoffs_and_all_real_prizes(self):
        with patch.object(arcade.secrets, "randbelow", side_effect=range(100)):
            self.assertEqual(Counter(arcade.draw_prize() for _ in range(100)),
                             {"codex_credits": 1, "event_ticket": 8, "roo_points": 56, "none": 35})


@override_settings(**FLAGS)
class ArcadeTests(TestCase):
    def setUp(self):
        self.member = get_user_model().objects.create_user(email="arcade-member@example.test")
        self.other = get_user_model().objects.create_user(email="arcade-other@example.test")
        self.client = APIClient()
        self.client.force_authenticate(self.member)

    def test_new_publication_once_and_only_for_its_author(self):
        org = Organization.objects.create(name="Arcade startup", domain="arcade.example.test")
        draft = MonthlyUpdateDraft.objects.create(organization=org, month=date(2026, 9, 1))
        company = type("OwnedCompany", (), {"organization_id": org.pk})()
        with self.assertRaises(ValueError):
            award_completion(self.member, company, draft, newly_approved=True)
        self.assertFalse(arcade.rounds_for(self.member).exists())
        draft.published_at = draft.first_published_at = timezone.now()
        draft.save(update_fields=["published_at", "first_published_at"])
        award_completion(self.member, company, draft, newly_approved=True)
        award_completion(self.other, company, draft, newly_approved=False)
        self.assertEqual(arcade.rounds_for(self.member).get().source, {"trigger": "pulse"})
        self.assertFalse(arcade.rounds_for(self.other).exists())

    def test_level_jump_uses_profile_thresholds_independent_of_bonuses(self):
        # Exactly 25 Roo reaches profile ladder level two (12 then 13).
        ledger, _ = PointsService.award(
            user=self.member, delta=25, source="TASK", description="Reviewed contribution",
            created_by_slack_id="reviewer", reference_type="TASK_ASSIGNMENT",
            reference_id="synthetic", idempotency_key="arcade:synthetic:contribution",
        )
        record_wallet_credit(ledger)
        record_wallet_credit(ledger)
        rounds = arcade.rounds_for(self.member).order_by("source__level")
        self.assertEqual([row.source for row in rounds],
                         [{"trigger": "level", "level": 1}, {"trigger": "level", "level": 2}])
        with patch.object(arcade, "draw_prize", return_value="roo_points"):
            arcade.spin(self.member, rounds[0].pk)
        self.assertEqual(contribution_total(self.member), 25_000_000)
        self.assertEqual(rounds.count(), 2)

    def test_direct_rank_credit_crossing_also_grants_round(self):
        for i in range(3):
            PointsService.award(user=self.member, delta=4, source="EVENT",
                                description="First post in channel example", created_by_slack_id="system",
                                idempotency_key=f"arcade:test:post:{i}")
        self.assertEqual(arcade.rounds_for(self.member).get().source, {"trigger": "level", "level": 1})

    def test_round_ownership_replay_and_acknowledgement(self):
        row = arcade.earn_pulse_round(self.member, 123)
        other = arcade.earn_pulse_round(self.other, 456)
        self.assertEqual(self.client.get(API).data["rounds"],
                         [{"id": str(row.pk), "trigger": "pulse", "prize": None}])
        self.assertEqual(self.client.post(f"{API}{other.pk}/spin/").status_code, 404)
        self.assertEqual(self.client.post(f"{API}{row.pk}/acknowledge/").status_code, 409)
        with patch.object(arcade, "draw_prize", return_value="roo_points") as draw:
            first = self.client.post(f"{API}{row.pk}/spin/", {"prize": "codex_credits"})
            again = self.client.post(f"{API}{row.pk}/spin/")
            self.assertEqual((first.status_code, again.data), (200, first.data))
            draw.assert_called_once()
        self.assertEqual(first.data["points"], 5)
        self.assertEqual(first["Cache-Control"], "private, no-store")
        self.assertEqual(self.client.get(API).data["rounds"], [first.data])
        self.assertEqual(PointsAccount.objects.get(user=self.member).balance, 5)
        for _ in range(2):
            self.assertEqual(self.client.post(f"{API}{row.pk}/acknowledge/").status_code, 204)
        self.assertEqual(self.client.get(API).data["rounds"], [])
        self.assertEqual(self.client.post(f"{API}{row.pk}/spin/").data, first.data)
        self.client.force_authenticate(user=None)
        self.assertIn(self.client.get(API).status_code, (401, 403))

    def test_missing_stock_substitutes_wallet_credit_and_a_miss_pays_nothing(self):
        with patch.object(arcade, "draw_prize", side_effect=["codex_credits", "event_ticket", "none"]):
            results = [arcade.spin(self.member, arcade.earn_pulse_round(self.member, i).pk) for i in range(3)]
        self.assertEqual([r["prize"] for r in results], ["roo_points", "roo_points", "none"])
        self.assertEqual(PointsAccount.objects.get(user=self.member).balance, 10)

    @override_settings(SYSTEM_EMAIL_RESEND_API_KEY="synthetic-test-only",
                       MLAI_SYSTEM_FROM_EMAIL="MLAI <prizes@example.test>")
    def test_level_up_to_real_codex_reservation_and_rendered_email(self):
        from community_chat.arcade_inventory import import_stock
        from crm.arcade_delivery import run_arcade_prize_delivery
        from crm.models import CrmAuditEvent
        from system_email.models import SystemEmail

        self.other.is_superuser = True
        self.other.save(update_fields=["is_superuser"])
        self.member.email_verified_at = timezone.now()
        self.member.save(update_fields=["email_verified_at"])
        import_stock(self.other, {"prize": "codex", "codes": ["SYNTHETIC-WINNER-ONLY"]})
        ledger, _ = PointsService.award(
            user=self.member, delta=12, source="TASK", description="Reviewed contribution",
            created_by_slack_id="reviewer", reference_type="TASK_ASSIGNMENT",
            reference_id="synthetic-e2e", idempotency_key="arcade:e2e:threshold",
        )
        record_wallet_credit(ledger)
        earned, = self.client.get(API).data["rounds"]
        self.assertEqual((earned["trigger"], earned["level"], earned["prize"]), ("level", 1, None))
        spin_url = f"{API}{earned['id']}/spin/"
        with patch.object(arcade, "draw_prize", return_value="codex_credits") as draw:
            result = self.client.post(spin_url)
            self.assertEqual(result.data, {**earned, "prize": "codex_credits"})
            self.assertEqual(self.client.post(spin_url).data, result.data)
            self.assertEqual(self.client.get(API).data["rounds"], [result.data])
            draw.assert_called_once()
        with patch("system_email.transport_resend.send", return_value="synthetic-email-id") as provider:
            run_arcade_prize_delivery()
            run_arcade_prize_delivery()
            provider.assert_called_once()
        payload = provider.call_args.args[0]
        self.assertEqual(payload["to"], [self.member.email])
        for body in ("text", "html"):
            self.assertIn("SYNTHETIC-WINNER-ONLY", payload[body])
            self.assertIn("https://chatgpt.com/codex/p/", payload[body])
        self.assertEqual(SystemEmail.objects.get().provider_message_id, "synthetic-email-id")
        self.assertTrue(CrmAuditEvent.objects.filter(target_id=earned["id"], detail__state="sent").exists())

    def test_ticket_round_replays_the_same_members_reserved_coupon(self):
        from community_chat.arcade_inventory import import_stock

        self.other.is_superuser = True
        self.other.save(update_fields=["is_superuser"])
        import_stock(self.other, {"prize": "ticket", "codes": ["SYNTHETIC-HALF"],
                                  "eventName": "MLAI demo night", "eventUrl": "https://lu.ma/demo",
                                  "expiresAt": (timezone.now() + timedelta(days=7)).isoformat()})
        row = arcade.earn_pulse_round(self.member, 700)
        with patch.object(arcade, "draw_prize", return_value="event_ticket"):
            result = self.client.post(f"{API}{row.pk}/spin/")
            self.assertEqual(result.data["ticketCode"], "SYNTHETIC-HALF")
            self.assertEqual(result.data["eventUrl"], "https://lu.ma/demo")
            self.assertEqual(self.client.get(API).data["rounds"], [result.data])
            second = arcade.spin(self.other, arcade.earn_pulse_round(self.other, 701).pk)
        self.assertEqual(second["prize"], "roo_points")
        self.assertNotIn("ticketCode", second)
        row.refresh_from_db()
        self.assertNotIn("SYNTHETIC-HALF", str(row.metadata))

    def test_failed_wallet_write_preserves_entitlement(self):
        row = arcade.earn_pulse_round(self.member, 124)
        with patch.object(arcade, "draw_prize", return_value="roo_points"), \
             patch.object(PointsService, "award", side_effect=ValueError("Unavailable")), \
             self.assertRaises(ValueError):
            arcade.spin(self.member, row.pk)
        row.refresh_from_db()
        self.assertEqual((row.status, row.metadata), ("ready", {}))


@skipUnless(connection.vendor == "postgresql", "Requires database row locks")
@override_settings(**FLAGS)
class ConcurrentArcadeTests(TransactionTestCase):
    def test_simultaneous_pulls_credit_once(self):
        member = get_user_model().objects.create_user(email="arcade-race@example.test")
        row = arcade.earn_pulse_round(member, 99)
        barrier = Barrier(2, timeout=10)

        def pull():
            close_old_connections()
            try:
                barrier.wait()
                return arcade.spin(member, row.pk)
            finally:
                connections.close_all()

        with patch.object(arcade, "draw_prize", return_value="roo_points"), ThreadPoolExecutor(2) as pool:
            tickets = [pool.submit(pull) for _ in range(2)]
            self.assertEqual(tickets[0].result(timeout=15), tickets[1].result(timeout=15))
        self.assertEqual(Ledger.objects.filter(user=member, reference_type="MLAI_ARCADE").count(), 1)
        self.assertEqual(PointsAccount.objects.get(user=member).balance, 5)

    @override_settings(SYSTEM_EMAIL_RESEND_API_KEY="synthetic-test-only",
                       MLAI_SYSTEM_FROM_EMAIL="MLAI <prizes@example.test>")
    def test_two_members_can_only_reserve_one_codex_code_per_window(self):
        from community_chat.arcade_inventory import import_stock, stock_summary

        staff = get_user_model().objects.create_user(email="arcade-admin@example.test", is_superuser=True)
        import_stock(staff, {"prize": "codex", "codes": ["SYNTHETIC-RACE-A", "SYNTHETIC-RACE-B"]})
        entries = []
        for i in range(2):
            user = get_user_model().objects.create_user(
                email=f"arcade-race-{i}@example.test", email_verified_at=timezone.now())
            entries.append((user, arcade.earn_pulse_round(user, i)))
        barrier = Barrier(2, timeout=10)

        def pull(entry):
            close_old_connections()
            try:
                barrier.wait()
                return arcade.spin(entry[0], entry[1].pk)["prize"]
            finally:
                connections.close_all()

        with patch.object(arcade, "draw_prize", return_value="codex_credits"), ThreadPoolExecutor(2) as pool:
            draws = [pool.submit(pull, entry) for entry in entries]
            self.assertCountEqual([draw.result(timeout=15) for draw in draws], ["codex_credits", "roo_points"])
        self.assertEqual(stock_summary()["codex"], {"available": 1, "reserved": 1})
