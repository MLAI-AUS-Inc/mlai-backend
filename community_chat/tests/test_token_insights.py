import copy
import hashlib
from datetime import datetime, timedelta, timezone as tz

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from community_chat.models import TokenUsageAccount
from community_chat.token_insights import current_report, validate_report


def report(now=None, window="7d"):
    return {
        "status": "ready", "engineRevision": "da4de0cc2a55169dc4edd41cfbff8b66d74ecc44",
        "window": window, "assessedSessions": 10, "totalSessions": 12,
        "computedAt": (now or timezone.now()).isoformat(),
        "finding": {"detector": "sessions_over_depth", "affectedSessions": 2,
                    "estimatedTokenBurnBasisPoints": None, "estimatedSavingsUsd": None},
    }


class TokenInsightValidationTests(SimpleTestCase):
    def test_refuses_transcripts_unknown_detectors_and_impossible_claims(self):
        now = timezone.now()
        for change in (
            {"transcript": "private"}, {"assessedSessions": True}, {"totalSessions": 1},
            {"computedAt": (now - timedelta(minutes=11)).isoformat()},
            {"computedAt": (now + timedelta(minutes=2)).isoformat()},
            {"status": "disabled"}, {"engineRevision": "arbitrary text"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_report(report(now) | change, now=now)
        for change in (
            {"detector": "<script>"}, {"affectedSessions": 11}, {"prompt": "private"},
            {"estimatedTokenBurnBasisPoints": 10001}, {"estimatedSavingsUsd": float("nan")},
        ):
            value = report(now)
            value["finding"].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_report(value, now=now)
        self.assertEqual(validate_report(report(now), now=now), report(now))
        offset = now.astimezone(tz(timedelta(hours=10)))
        self.assertEqual(validate_report(report(offset), now=now)["computedAt"], now.isoformat())

    def test_today_expires_at_local_midnight_and_other_reports_after_a_day(self):
        now = datetime(2026, 10, 7, 13, 1, tzinfo=tz.utc)  # midnight Melbourne
        value = report(now - timedelta(minutes=2), "today")
        stored = {"today": {"timezone": "Australia/Melbourne", "report": value}}
        self.assertIsNone(current_report(stored, "today", "Australia/Melbourne", now=now))
        value = report(now - timedelta(hours=25))
        stored = {"7d": {"timezone": "Australia/Melbourne", "report": value}}
        self.assertIsNone(current_report(stored, "7d", "Australia/Melbourne", now=now))


@override_settings(TOKEN_USAGE_LEADERBOARD_TIME_ZONE="Australia/Melbourne")
class TokenInsightApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(email="tips@example.com")
        self.other = get_user_model().objects.create_user(email="other@example.com")
        self.account = TokenUsageAccount.objects.create(user=self.user, token_hash=hashlib.sha256(b"mlai_usage_test-reporter").hexdigest())
        self.client.force_authenticate(self.user)
        self.url = reverse("community_chat_token_insights")

    def upload(self, value=None):
        return self.client.post(self.url, {"timezone": "Australia/Melbourne", "report": value or report()}, format="json")

    def test_default_off_upload_requires_consent_and_disable_erases_all_windows(self):
        self.assertFalse(self.client.get(self.url).data["enabled"])
        self.assertEqual(self.upload().status_code, 409)
        self.assertEqual(self.client.patch(self.url, {"enabled": True}, format="json").status_code, 200)
        for window in ("today", "7d", "30d", "all"):
            self.assertEqual(self.upload(report(window=window)).status_code, 200)
        response = self.client.get(self.url + "?window=7d")
        self.assertEqual(response.data["report"]["finding"]["affectedSessions"], 2)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.client.patch(self.url, {"enabled": False}, format="json")
        self.account.refresh_from_db()
        self.assertEqual(self.account.insights_reports, {})
        self.assertEqual(self.upload().status_code, 409)

    def test_other_members_reporter_tokens_and_anonymous_callers_cannot_read(self):
        self.client.patch(self.url, {"enabled": True}, format="json")
        self.upload()
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(self.url).data["report"], None)
        self.assertEqual(self.upload().status_code, 409)
        self.client.force_authenticate(None)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer mlai_usage_test-reporter")
        self.assertIn(self.client.get(self.url).status_code, (401, 403))
        self.client.credentials()
        self.assertIn(self.client.get(self.url).status_code, (401, 403))

    def test_rejects_extra_fields_and_preserves_newer_report(self):
        self.client.patch(self.url, {"enabled": True}, format="json")
        now = timezone.now()
        newer = report(now)
        self.assertTrue(self.upload(newer).data["accepted"])
        older = report(now - timedelta(minutes=1))
        self.assertFalse(self.upload(older).data["accepted"])
        bad = copy.deepcopy(newer)
        bad["finding"]["transcript"] = "private"
        self.assertEqual(self.upload(bad).status_code, 400)
        self.assertEqual(self.client.get(self.url + "?window=7d").data["report"], newer)
        self.assertEqual(self.client.get(self.url + "?window=invalid").status_code, 400)
        self.assertEqual(self.client.patch(self.url, {"enabled": "true"}, format="json").status_code, 400)

    def test_leaving_leaderboard_deletes_tips_too(self):
        self.client.patch(self.url, {"enabled": True}, format="json")
        self.upload()
        self.client.delete(reverse("token_usage_token"))
        self.assertFalse(self.client.get(self.url).data["enabled"])
