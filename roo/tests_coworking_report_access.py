"""Report authorization checks without database, Slack or report-data access."""

from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from .views import CoworkingViewSet


@override_settings(
    ROO_API_KEY="synthetic-roo-report-key",
    INTERNAL_API_KEY="synthetic-internal-key",
    MLAI_API_KEY="synthetic-website-key",
    COWORKING_REPORT_SLACK_TEAM_ID="TSHARED",
    COWORKING_REPORT_SLACK_CHANNEL_ID="CSHARED",
    POINTS_BOOTSTRAP_ADMIN_SLACK_IDS=[],
)
class CoworkingReportChannelAccessTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = CoworkingViewSet.as_view({"get": "report"})
        role_patch = patch("roo.permissions._active_admin_with_role_exists", return_value=False)
        self.roles = role_patch.start()
        self.addCleanup(role_patch.stop)
        report_patch = patch("roo.views.CoworkingService.build_report", return_value={
            "source": "active_coworking_bookings", "totals": {"booked_user_days": 3},
        })
        self.report = report_patch.start()
        self.addCleanup(report_patch.stop)

    def request(self, *, key="synthetic-roo-report-key", **overrides):
        params = {
            "slack_user_id": "USTAFF", "start_date": "2026-01-01", "end_date": "2026-01-31",
            "slack_team_id": "TSHARED", "slack_channel_id": "CSHARED",
        }
        params.update(overrides)
        return self.view(self.factory.get("/api/v1/points/coworking/report/", params, HTTP_X_API_KEY=key))

    def test_ordinary_member_can_read_report_in_configured_chat(self):
        response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["totals"], {"booked_user_days": 3})

    def test_other_channels_and_dms_cannot_use_channel_grant(self):
        for channel in ("COTHER", "DSHARED", "", "CSHARED, COTHER"):
            with self.subTest(channel=channel):
                self.assertEqual(self.request(slack_channel_id=channel).status_code, 403)
        self.report.assert_not_called()

    def test_wrong_or_missing_workspace_cannot_use_channel_grant(self):
        for team in ("TOTHER", "", "TSHARED, TOTHER"):
            with self.subTest(team=team):
                self.assertEqual(self.request(slack_team_id=team).status_code, 403)
        self.report.assert_not_called()

    def test_channel_grant_requires_real_slack_actor(self):
        for actor in ("", "bridge-display-name", "B123", "<@USTAFF>"):
            with self.subTest(actor=actor):
                self.assertIn(self.request(slack_user_id=actor).status_code, (400, 403))
        self.report.assert_not_called()

    def test_other_service_credentials_cannot_assert_channel_access(self):
        for key in ("synthetic-internal-key", "synthetic-website-key", "invalid-key", ""):
            with self.subTest(key=key):
                self.assertIn(self.request(key=key).status_code, (401, 403))
        self.report.assert_not_called()

    def test_unconfigured_or_malformed_channel_grant_is_disabled(self):
        for team, channel in (("", "CSHARED"), ("TSHARED", ""), ("TSHARED", "DSHARED"),
                              ("TSHARED,COTHER", "CSHARED"), ("TSHARED", "CSHARED,COTHER")):
            with self.subTest(team=team, channel=channel), override_settings(
                COWORKING_REPORT_SLACK_TEAM_ID=team, COWORKING_REPORT_SLACK_CHANNEL_ID=channel,
            ):
                self.assertEqual(self.request(slack_team_id=team, slack_channel_id=channel).status_code, 403)
        self.report.assert_not_called()

    def test_existing_admin_and_partner_access_does_not_require_channel_context(self):
        for role in ("admin", "committee", "portfolio_lead", "partner"):
            with self.subTest(role=role):
                self.roles.side_effect = lambda actor, allowed_roles: role in allowed_roles
                response = self.request(slack_team_id="", slack_channel_id="DPRIVATE")
                self.assertEqual(response.status_code, 200)

    def test_channel_grant_keeps_required_dates(self):
        self.assertEqual(self.request(start_date="").status_code, 400)
        self.report.assert_not_called()

    def test_report_grant_does_not_grant_points_or_luma_permissions(self):
        from .permissions import can_export_luma_attendees, is_points_admin

        self.assertEqual(self.request().status_code, 200)
        self.assertFalse(is_points_admin("USTAFF"))
        self.assertFalse(can_export_luma_attendees("USTAFF"))
