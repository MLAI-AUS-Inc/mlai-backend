import hashlib
import hmac
import secrets
import time

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from .models import VictorApplication, VictorApplicationAccessAudit
from .roo_auth import canonical_actor_payload


SECRET = "victor-roo-test-secret-that-is-at-least-thirty-two-characters"
TEAM_ID = "TMLAI123"
CHANNEL_ID = "GVICTOR123"


def application(**overrides):
    sequence = VictorApplication.objects.count() + 1
    values = {
        "client_ref": f"victor-{sequence}",
        "stage": VictorApplication.STAGE_COMPLETE,
        "first_name": "Jordan",
        "last_name": "Taylor",
        "email": f"jordan{sequence}@example.com",
        "linkedin": "https://linkedin.com/in/jordantaylor",
        "team_name": "Team Sunrise",
        "role": "Founder",
        "startup_stage": "Prototype / MVP",
        "industry_sector": "Software & Enterprise",
        "location": "Adelaide, AU",
        "team_size": 2,
        "team_members": [
            {
                "first_name": "Alex",
                "last_name": "Chen",
                "email": "alex@example.com",
                "role": "CTO",
            }
        ],
        "revenue_last_3_months": {"2026-05": 100, "2026-06": 200, "2026-07": 300},
        "idea": "An AI copilot for grant applications.",
        "support": "Introductions to mentors.",
        "consent": True,
    }
    values.update(overrides)
    return VictorApplication.objects.create(**values)


def signed_headers(
    *,
    team_id=TEAM_ID,
    channel_id=CHANNEL_ID,
    user_id="UANYMEMBER",
    timestamp=None,
    nonce=None,
    event_id=None,
    request_id=None,
    secret=SECRET,
):
    timestamp = int(time.time()) if timestamp is None else int(timestamp)
    nonce = nonce or secrets.token_urlsafe(24)
    event_id = event_id or f"Ev-{secrets.token_hex(8)}"
    request_id = request_id or f"roo-{secrets.token_hex(8)}"
    values = {
        "surface": "public_roo",
        "slack_team_id": team_id,
        "acting_slack_user_id": user_id,
        "slack_channel_id": channel_id,
        "slack_thread_ts": "1234567890.123456",
        "event_id": event_id,
        "request_id": request_id,
        "timestamp": timestamp,
        "nonce": nonce,
    }
    signature = "v1=" + hmac.new(
        secret.encode("utf-8"),
        canonical_actor_payload(**values),
        hashlib.sha256,
    ).hexdigest()
    return {
        "HTTP_X_VICTOR_ROO_SIGNATURE": signature,
        "HTTP_X_VICTOR_ROO_TIMESTAMP": str(timestamp),
        "HTTP_X_VICTOR_ROO_NONCE": nonce,
        "HTTP_X_ROO_SURFACE": values["surface"],
        "HTTP_X_SLACK_TEAM_ID": team_id,
        "HTTP_X_ACTING_SLACK_USER_ID": user_id,
        "HTTP_X_SLACK_CHANNEL_ID": channel_id,
        "HTTP_X_SLACK_THREAD_TS": values["slack_thread_ts"],
        "HTTP_X_SLACK_EVENT_ID": event_id,
        "HTTP_X_REQUEST_ID": request_id,
    }


@override_settings(
    VICTOR_AI_ROO_ENABLED=True,
    VICTOR_AI_ROO_SIGNING_SECRET=SECRET,
    VICTOR_AI_ROO_ASSERTION_MAX_AGE_SECONDS=60,
    VICTOR_AI_ROO_ASSERTION_CLOCK_SKEW_SECONDS=5,
    VICTOR_AI_ROO_EXPORT_MAX_ROWS=5000,
)
class VictorRooAccessTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_summary_distinguishes_complete_applications_from_leads(self):
        application()
        application(
            client_ref="lead-1",
            stage=VictorApplication.STAGE_LEAD,
            email="lead@example.com",
            team_name="",
            team_size=None,
            team_members=[],
            revenue_last_3_months={},
            idea="",
            support="",
            consent=False,
        )

        response = self.client.get(
            "/api/v1/victor-ai/roo/applications/summary/",
            **signed_headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total_records"], 2)
        self.assertEqual(response.json()["complete_count"], 1)
        self.assertEqual(response.json()["lead_count"], 1)
        self.assertEqual(response.json()["complete_created_today"], 1)
        audit = VictorApplicationAccessAudit.objects.get()
        self.assertEqual(audit.action, "summary")
        self.assertEqual(audit.acting_slack_user_id, "UANYMEMBER")
        self.assertEqual(audit.row_count, 2)

    def test_any_channel_member_can_list_without_backend_user_mapping(self):
        row = application()

        response = self.client.get(
            "/api/v1/victor-ai/roo/applications/",
            **signed_headers(user_id="UGUEST123"),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total_count"], 1)
        self.assertEqual(body["applications"][0]["id"], row.pk)
        self.assertNotIn("client_ref", body["applications"][0])

    def test_list_supports_filters_and_pagination(self):
        application(industry_sector="Education", email="edu@example.com")
        application(client_ref="other", industry_sector="Health", email="health@example.com")

        response = self.client.get(
            "/api/v1/victor-ai/roo/applications/",
            {"industry_sector": "education", "limit": 1, "offset": 0},
            **signed_headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total_count"], 1)
        self.assertEqual(response.json()["applications"][0]["email"], "edu@example.com")

    def test_detail_returns_full_business_record_without_client_ref(self):
        row = application()

        response = self.client.get(
            f"/api/v1/victor-ai/roo/applications/{row.pk}/",
            **signed_headers(),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["idea"], row.idea)
        self.assertEqual(body["team_members"][0]["email"], "alex@example.com")
        self.assertEqual(body["revenue_last_3_months"]["2026-06"], 200)
        self.assertNotIn("client_ref", body)

    def test_csv_export_is_formula_safe_and_audited(self):
        application(first_name="=HYPERLINK(\"https://bad.test\")")

        response = self.client.get(
            "/api/v1/victor-ai/roo/applications/export.csv",
            **signed_headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("victor-ai-applications-", response["Content-Disposition"])
        content = response.content.decode("utf-8")
        self.assertIn("'=HYPERLINK", content)
        self.assertNotIn("client_ref", content.splitlines()[0])
        audit = VictorApplicationAccessAudit.objects.get()
        self.assertEqual(audit.action, "export_csv")
        self.assertEqual(audit.row_count, 1)

    @override_settings(VICTOR_AI_ROO_EXPORT_MAX_ROWS=1)
    def test_csv_export_requires_narrower_filters_over_row_limit(self):
        application()
        application(client_ref="second", email="second@example.com")

        response = self.client.get(
            "/api/v1/victor-ai/roo/applications/export.csv",
            **signed_headers(),
        )

        self.assertEqual(response.status_code, 413)
        audit = VictorApplicationAccessAudit.objects.get()
        self.assertEqual(audit.outcome, "too_large")

    def test_signed_request_needs_no_backend_slack_allowlist(self):
        application()

        response = self.client.get(
            "/api/v1/victor-ai/roo/applications/",
            **signed_headers(channel_id="GOTHER123"),
        )

        self.assertEqual(response.status_code, 200)
        audit = VictorApplicationAccessAudit.objects.get()
        self.assertEqual(audit.slack_channel_id, "GOTHER123")

    def test_generic_roo_api_key_cannot_replace_signed_context(self):
        response = self.client.get(
            "/api/v1/victor-ai/roo/applications/",
            HTTP_X_API_KEY="generic-roo-key",
        )

        self.assertEqual(response.status_code, 403)

    def test_tampered_signature_is_denied(self):
        headers = signed_headers()
        headers["HTTP_X_ACTING_SLACK_USER_ID"] = "UATTACKER"

        response = self.client.get(
            "/api/v1/victor-ai/roo/applications/",
            **headers,
        )

        self.assertEqual(response.status_code, 403)

    def test_signed_request_cannot_be_replayed(self):
        headers = signed_headers()

        first = self.client.get("/api/v1/victor-ai/roo/applications/", **headers)
        second = self.client.get("/api/v1/victor-ai/roo/applications/", **headers)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 403)

    def test_expired_signature_is_denied(self):
        response = self.client.get(
            "/api/v1/victor-ai/roo/applications/",
            **signed_headers(timestamp=int(time.time()) - 120),
        )

        self.assertEqual(response.status_code, 403)


class VictorRooDisabledTests(TestCase):
    @override_settings(
        VICTOR_AI_ROO_ENABLED=False,
        VICTOR_AI_ROO_SIGNING_SECRET=SECRET,
    )
    def test_feature_is_fail_closed_when_disabled(self):
        response = APIClient().get(
            "/api/v1/victor-ai/roo/applications/",
            **signed_headers(),
        )
        self.assertEqual(response.status_code, 403)
