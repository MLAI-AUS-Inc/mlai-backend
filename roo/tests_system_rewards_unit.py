"""Database-free canonical system reward and API regressions."""

from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory
from roo.system_rewards import native_reward_user, canonical_link_love_key
from roo.views import SystemAwardView

PROFILE = "00000000-0000-4000-8000-000000000001"
PREFIX = "link_love:CBOOST:1793842044.896789"
KEY = f"{PREFIX}:profile:{PROFILE}"
MODULE = "roo.system_rewards"


@override_settings(ROO_API_KEY="synthetic-roo-key", INTERNAL_API_KEY="synthetic-internal-key")
class SystemRewardTests(SimpleTestCase):
    def setUp(self):
        self.user = SimpleNamespace(pk=42, community_chat_profile_id=PROFILE, full_name="Chat Member", slack_id="USAM")
        users = self.enterContext(patch(f"{MODULE}.User.objects"))
        users.filter.return_value.first.return_value = self.user
        self.devices = self.enterContext(patch(f"{MODULE}.CommunityChatDevice.objects"))
        self.devices.filter.return_value.exists.return_value = True
        self.consent = self.enterContext(patch(f"{MODULE}.has_ai_consent", return_value=True))
        self.ledger = self.enterContext(patch(f"{MODULE}.Ledger.objects"))
        self.ledger.filter.return_value.order_by.return_value.first.return_value = None
        self.ledger.filter.return_value.first.return_value = None
        self.enterContext(patch(f"{MODULE}._bridge_root_ids", return_value=[]))
        self.enterContext(patch(f"{MODULE}.prepare_link_love_award"))
        # API unit tests isolate the transaction wrapper; database suites retain it.
        self.enterContext(patch.object(SystemAwardView, "post", SystemAwardView.post.__wrapped__))

    def test_native_recipient_needs_canonical_id_device_consent_and_fixed_reward(self):
        self.assertIs(native_reward_user(PROFILE, points=2, reason="link-love", idempotency_key=KEY), self.user)
        for profile, points, reason, key in (("name", 2, "link-love", KEY),
                (PROFILE, 100, "link-love", KEY), (PROFILE, 2, "other", KEY), (PROFILE, 2, "link-love", "")):
            with self.subTest(profile=profile, points=points, reason=reason), self.assertRaises(ValueError):
                native_reward_user(profile, points=points, reason=reason, idempotency_key=key)
        self.consent.return_value = False
        with self.assertRaises(ValueError): native_reward_user(PROFILE, points=2, reason="link-love", idempotency_key=KEY)
        self.consent.return_value = True
        self.devices.filter.return_value.exists.return_value = False
        with self.assertRaises(ValueError): native_reward_user(PROFILE, points=2, reason="link-love", idempotency_key=KEY)

    def test_slack_and_chat_aliases_share_one_canonical_key(self):
        for recipient in ("USAM", f"profile:{PROFILE}"):
            self.assertEqual(canonical_link_love_key(self.user, f"{PREFIX}:{recipient}", recipient=recipient,
                                                   points=2, reason="link-love"), KEY)

    def test_historical_slack_award_retains_existing_key(self):
        old = f"{PREFIX}:USAM"
        self.ledger.filter.return_value.order_by.return_value.first.return_value = SimpleNamespace(idempotency_key=old)
        self.assertEqual(canonical_link_love_key(self.user, KEY, recipient=f"profile:{PROFILE}", points=2, reason="link-love"), old)
        self.assertIn(old, self.ledger.filter.call_args.kwargs["idempotency_key__in"])

    def post(self, data, key="synthetic-roo-key"):
        return SystemAwardView.as_view()(APIRequestFactory().post("/award", data, format="json", HTTP_X_API_KEY=key))

    def test_native_api_awards_canonical_user_and_returns_safe_presentation_data(self):
        with (patch("roo.views.PointsService.award", return_value=(SimpleNamespace(id=7, delta=2), True)) as award,
              patch("roo.views.PointsService.get_balance", return_value={"balance": 10}),
              patch("roo.views.get_or_create_user_for_slack_id") as slack):
            result = self.post(dict(target_profile_id=PROFILE, points=2, reason="link-love", idempotency_key=KEY))
            self.assertEqual(result.status_code, 200)
            self.assertIs(award.call_args.kwargs["user"], self.user)
            self.assertEqual(award.call_args.kwargs["idempotency_key"], KEY)
            self.assertEqual(result.data["display_name"], "Chat Member")
            slack.assert_not_called()

    def test_native_api_rejects_general_internal_key_dual_targets_and_missing_operation(self):
        data = dict(target_profile_id=PROFILE, points=2, reason="link-love", idempotency_key=KEY)
        with patch("roo.views.PointsService.award") as award:
            self.assertEqual(self.post(data, "synthetic-internal-key").status_code, 403)
            self.assertEqual(self.post({**data, "target_slack_id": "USAM"}).status_code, 400)
            self.assertEqual(self.post({**data, "idempotency_key": ""}).status_code, 400)
            award.assert_not_called()

    def test_legacy_empty_slack_mention_preserves_existing_error_contract(self):
        with patch("roo.views.PointsService.award") as award:
            result = self.post(dict(target_slack_id="<@>", points=2, reason="link-love"))
        self.assertEqual(result.status_code, 400)
        self.assertEqual(result.data["error"], "target_slack_id and points are required")
        award.assert_not_called()
