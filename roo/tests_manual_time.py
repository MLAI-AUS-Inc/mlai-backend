from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from django.db import connections
from django.test import override_settings, skipUnlessDBFeature
from rest_framework.test import APIClient, APITestCase, APITransactionTestCase

from core.models import User
from roo.manual_time_rules import CATALOGUE
from roo.models import Ledger, PointsAccount, PointsAdmin, PointsRequest, PointsRequestDelivery


@override_settings(
    ROO_MANUAL_TIME_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_ENABLED=True,
    COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID="TTEST",
    ROO_API_KEY="test-manual-time-service-key",
)
class ManualTimeIntegrationTests(APITestCase):
    chat = "/api/v1/community-chat/volunteer/manual-time/"
    slack = "/api/v1/points/manual-time/"

    def setUp(self):
        self.member = User.objects.create_user(email="volunteer@example.test", slack_id="UMEMBER")
        self.admin = User.objects.create_user(email="reviewer@example.test", slack_id="UADMIN")
        PointsAdmin.objects.create(
            user=self.admin, slack_user_id="UADMIN", role="admin", weekly_allowance=25,
        )
        self.bot = APIClient()
        self.bot.credentials(HTTP_X_API_KEY="test-manual-time-service-key")
        self.payload = {
            "submission_key": str(uuid4()), "catalog_version": CATALOGUE["version"],
            "action_ids": ["event.door", "event.pack"], "volunteer_minutes": 45,
            "note": "private fixture note",
        }

    def service(self, op, **data):
        return self.bot.post(f"{self.slack}{op}/", {"workspace": "TTEST", **data}, format="json")

    @override_settings(COMMUNITY_CHAT_VOLUNTEER_ONE_RECORD_ENABLED=True)
    def test_chat_submit_slack_approve_and_retry(self):
        self.client.force_authenticate(self.member)
        response = self.client.post(self.chat, self.payload, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        request_id = response.data["id"]
        self.assertEqual(response.data["status"], "pending")
        self.assertNotIn("note", response.data)
        self.assertFalse(Ledger.objects.filter(user=self.member).exists())
        self.assertEqual(self.client.get(self.chat + "?mode=review").status_code, 403)

        packet = self.service("claim").data
        self.assertEqual(packet["request"]["id"], request_id)
        self.assertNotIn("note", packet["request"])
        message = {"channel": "CREVIEW", "ts": "1.02"}
        self.assertEqual(self.service("finish", **message, id=packet["id"], lease=packet["lease"]).status_code, 200)
        response = self.service("approve", **message, request_id=request_id, actor="UADMIN")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["roo"], "4.5")
        self.assertEqual(PointsAccount.objects.get(user=self.member).balance_microroo, 4_500_000)
        self.client.force_authenticate(self.admin)
        retry = self.client.post(f"{self.chat}{request_id}/", {}, format="json")
        self.assertEqual(retry.data["ledger_id"], response.data["ledger_id"])
        self.assertEqual(Ledger.objects.filter(user=self.member, reference_type="POINTS_REQUEST").count(), 1)
        receipt = self.service("claim").data
        self.assertEqual(receipt["stage"], "time_slack_receipt")
        self.assertEqual(receipt["target"], self.member.slack_id)
        self.assertEqual(receipt["request"]["ledger_id"], response.data["ledger_id"])
        self.assertNotIn("note", receipt["request"])
        self.client.force_authenticate(self.member)
        history = self.client.get("/api/v1/community-chat/volunteer/point-history/?scope=wallet")
        self.assertEqual(history.status_code, 200, history.data)
        self.assertEqual([row["amount_roo"] for row in history.data["results"]], ["4.5"])
        contributions = self.client.get("/api/v1/community-chat/volunteer/point-history/")
        self.assertEqual(contributions.status_code, 200, contributions.data)
        self.assertEqual(contributions.data["total_roo"], "4.5")
        self.assertEqual([row["amount_roo"] for row in contributions.data["results"]], ["4.5"])

    def test_slack_submit_chat_approve_uses_same_record(self):
        response = self.service("request", actor="UMEMBER", **self.payload)
        self.assertEqual(response.status_code, 200, response.data)
        request_id = response.data["id"]
        duplicate = self.service("request", actor="UMEMBER", **self.payload)
        self.assertEqual(duplicate.data["id"], request_id)
        conflict = self.service("request", actor="UMEMBER", **{**self.payload, "volunteer_minutes": 240})
        self.assertEqual(conflict.status_code, 409)
        self.client.force_authenticate(self.admin)
        queue = self.client.get(self.chat + f"?mode=review&request={request_id}")
        self.assertEqual(queue.data["requests"][0]["note"], "private fixture note")
        approved = self.client.post(f"{self.chat}{request_id}/", {}, format="json")
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertEqual(approved.data["roo"], "4.5")
        self.assertEqual(PointsRequest.objects.filter(manual_time_key__isnull=False).count(), 1)
        self.assertEqual(PointsAccount.objects.get(user=self.member).balance_microroo, 4_500_000)

    def test_direct_awards_charge_exact_remaining_allowance(self):
        award = self.service("award", actor="UADMIN", target="UMEMBER", **{**self.payload, "volunteer_minutes": 240})
        self.assertEqual(award.status_code, 200, award.data)
        self.assertEqual(award.data["roo"], "24")
        second = self.service(
            "award", actor="UADMIN", target="UMEMBER",
            **{**self.payload, "submission_key": str(uuid4()), "volunteer_minutes": 11},
        )
        self.assertEqual(second.status_code, 409, second.data)
        self.assertEqual(PointsAccount.objects.get(user=self.member).balance_microroo, 24_000_000)
        self.assertEqual(PointsRequest.objects.filter(manual_time_key__isnull=False).count(), 1)


@override_settings(
    ROO_MANUAL_TIME_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_ENABLED=True,
    COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID="TTEST",
    ROO_API_KEY="test-manual-time-service-key",
)
class ConcurrentManualTimeTests(APITransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_slack_and_chat_can_approve_concurrently(self):
        member = User.objects.create_user(email="race-member@example.test", slack_id="UMEMBER")
        admin = User.objects.create_user(email="race-admin@example.test", slack_id="UADMIN")
        PointsAdmin.objects.create(user=admin, slack_user_id=admin.slack_id, role="admin")
        self.client.force_authenticate(member)
        submitted = self.client.post(ManualTimeIntegrationTests.chat, {
            "submission_key": str(uuid4()), "catalog_version": CATALOGUE["version"],
            "action_ids": ["event.door"], "volunteer_minutes": 135,
        }, format="json")
        self.assertEqual(submitted.status_code, 200, submitted.data)
        request_id = submitted.data["id"]
        PointsRequest.objects.filter(pk=request_id).update(
            slack_channel_id="CREVIEW", slack_summary_message_ts="1.02",
        )
        ready = Barrier(2)

        def decide(slack):
            client = APIClient()
            try:
                ready.wait(timeout=10)
                if slack:
                    client.credentials(HTTP_X_API_KEY="test-manual-time-service-key")
                    return client.post(ManualTimeIntegrationTests.slack + "approve/", {
                        "workspace": "TTEST", "actor": admin.slack_id, "request_id": request_id,
                        "channel": "CREVIEW", "ts": "1.02",
                    }, format="json")
                client.force_authenticate(admin)
                return client.post(f"{ManualTimeIntegrationTests.chat}{request_id}/", {}, format="json")
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(decide, [True, False]))
        for response in responses:
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["roo"], "13.5")
        ledger = Ledger.objects.get(user=member, reference_type="POINTS_REQUEST")
        self.assertEqual([r.data["ledger_id"] for r in responses], [ledger.pk] * 2)
        self.assertEqual(PointsAccount.objects.get(user=member).balance_microroo, 13_500_000)
        self.assertEqual(PointsRequestDelivery.objects.filter(
            request_id=request_id, stage="time_slack_receipt",
        ).count(), 1)
