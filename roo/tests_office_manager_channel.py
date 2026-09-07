"""Pilot boundary tests: SimpleTestCase forbids database access/migrations."""
from datetime import date
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from . import office_manager as workflow
from .office_manager_policy import OFFICE_MANAGER_TEST_CHANNEL_ID as CHANNEL
from .views import CoworkingViewSet


@override_settings(
    ROO_API_KEY="pilot-only-roo-service-key",
    INTERNAL_API_KEY="distinct-internal-test-key",
    MLAI_API_KEY="distinct-mlai-test-key",
    OFFICE_MANAGER_ENABLED=True,
    OFFICE_MANAGER_SLACK_CHANNEL_ID=CHANNEL,
    OFFICE_MANAGER_SLACK_BOT_TOKEN="xoxb-synthetic-test-token",
)
class OfficeManagerChannelTests(SimpleTestCase):
    def request(self, channel):
        payload = {
            "slack_user_id": "UTESTER",
            "date": "2026-09-07",
            "attempt_id": "11111111-1111-4111-8111-111111111111",
            "generation": 1,
        }
        if channel is not None:
            payload["slack_channel_id"] = channel
        request = APIRequestFactory().post(
            "/api/v1/points/coworking/office-manager/claim/", payload,
            format="json", HTTP_X_API_KEY="pilot-only-roo-service-key",
        )
        return CoworkingViewSet.as_view({"post": "office_manager_claim"})(request)

    def test_api_rejects_missing_other_dm_and_malformed_channels_before_claim(self):
        for channel in [None, "", "CCOWORK", "DTESTER", "COTHER", CHANNEL + " ", [CHANNEL]]:
            with self.subTest(channel=channel), patch.object(workflow.OfficeManagerService, "claim") as claim:
                response = self.request(channel)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.data["code"], "channel_not_allowed")
                claim.assert_not_called()

    def test_allowed_api_reaches_authoritative_claim_service(self):
        with patch.object(workflow.OfficeManagerService, "claim", side_effect=workflow.OfficeManagerClaimError(
            "already_claimed", "The test role was already claimed"
        )) as claim:
            response = self.request(CHANNEL)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "already_claimed")
        claim.assert_called_once()

    def test_service_blocks_old_day_before_replay_identity_or_booking_mutations(self):
        with patch.object(workflow.OfficeManagerDay, "objects") as days, patch.object(
            workflow.OfficeManagerService, "resolve_member"
        ) as resolve, patch.object(workflow.OfficeManagerClaimAttempt, "objects") as attempts:
            days.filter.return_value.values_list.return_value.first.return_value = "CCOWORK"
            with self.assertRaises(workflow.OfficeManagerClaimError) as error:
                workflow.OfficeManagerService.claim(slack_user_id="UTESTER", booking_date=date(2026, 9, 7))
            self.assertEqual(error.exception.code, "channel_not_allowed")
            resolve.assert_not_called()
            attempts.filter.assert_not_called()

    def test_delivery_entrypoints_pause_old_records_even_when_creation_disabled(self):
        day_methods = ["post_announcement", "recover_announcement_coordinates", "reconcile_message"]
        assignment_methods = [
            "recover_winner_channel_coordinates", "deliver_winner_channel_announcement",
            "retract_winner_channel_announcement", "deliver_winner_dm",
            "deliver_end_of_day_reminder", "deliver_private_correction",
        ]
        for enabled in (True, False):
            for model, methods, keyword in [
                (workflow.OfficeManagerDay, day_methods, "day_id"),
                (workflow.OfficeManagerAssignment, assignment_methods, "assignment_id"),
            ]:
                for method in methods:
                    with self.subTest(enabled=enabled, method=method), override_settings(
                        OFFICE_MANAGER_ENABLED=enabled
                    ), patch.object(model, "objects") as records, patch.object(workflow.SlackService, "get_client") as slack:
                        records.filter.return_value.values_list.return_value.first.return_value = "CCOWORK"
                        self.assertFalse(getattr(workflow.OfficeManagerService, method)(**{keyword: 42}))
                        slack.assert_not_called()
                        records.select_for_update.assert_not_called()
                        records.filter.return_value.update.assert_not_called()

    def test_slack_boundary_rechecks_persisted_channel_before_provider_access(self):
        with patch.object(workflow.SlackService, "get_client") as slack:
            with self.assertRaises(workflow.OfficeManagerConfigurationError):
                workflow._office_manager_slack_client(channel_id="CCOWORK")
            slack.assert_not_called()
            self.assertIs(workflow._office_manager_slack_client(channel_id=CHANNEL), slack.return_value)
            slack.assert_called_once()

    def test_scheduler_fails_closed_for_nonpilot_configuration_before_database_work(self):
        for channel in ["CCOWORK", "COTHER", ""]:
            with self.subTest(channel=channel), override_settings(OFFICE_MANAGER_SLACK_CHANNEL_ID=channel):
                for dry_run in (True, False):
                    result = workflow.run_office_manager_scheduler(dry_run=dry_run)
                    self.assertEqual(result["status"], "failed")
                    self.assertEqual(result["reason"], "channel_not_allowed" if channel else "channel_not_configured")

    def test_preflight_advertises_pilot_contract_while_feature_is_disabled(self):
        request = APIRequestFactory().get(
            "/api/v1/points/coworking/office-manager/preflight/",
            HTTP_X_API_KEY="pilot-only-roo-service-key",
        )
        with override_settings(OFFICE_MANAGER_ENABLED=False):
            response = CoworkingViewSet.as_view({"get": "office_manager_preflight"})(request)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["claim_channel_required"])
        self.assertEqual(response.data["allowed_channel_id"], CHANNEL)
        self.assertFalse(response.data["enabled"])
