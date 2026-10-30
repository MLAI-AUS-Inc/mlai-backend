"""P1.11 (IF-P1-18): USD amounts, SHAs and operation ids are staff-only on the new journey.

For a non-staff founder of a new-journey organisation, the website cost usage
read (direct and through the My startup facade) keeps counts, coverage and
points and drops USD, SHAs and operation UUIDs. Staff and the legacy cohort
see today's payload, with every P1 flag on.
"""
from copy import deepcopy
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from . import website_cost_ledger as ledger
from .founder_journey_testing import ALL_P1_FLAGS_ON, NAMED_LEGACY_ORGS, make_named_legacy_org, make_new_journey_org
from .website_views import founder_cost_usage

URLS = ("/api/v1/vibe-marketing/website-connection/usage/",
        "/api/v1/my-startup/vibe-marketing/website-connection/usage/")
GOAL_ID = "8b0f2c1e-3d4a-4e5f-9a6b-7c8d9e0f1a2b"
SHA = "a" * 40
WITHOUT_V2 = {**ALL_P1_FLAGS_ON, "MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED": False}


def metric(total, basis):
    return {"basis": basis, "total": total, "knownSubtotal": total, "measuredRuns": 1, "recordedRuns": 1}


# The shape website_cost_usage.aggregate and website_cost_ledger.read return,
# plus the SHA and operation fields a receipt can carry.
USAGE = {
    "version": 1, "history": "retained_recorded_usage", "coverage": "observed_goal_meter_runs",
    "observedAt": "2026-10-30T00:00:00+00:00", "recordedRuns": 1, "incompleteRuns": 0, "truncated": False,
    "coverageIncomplete": False, "companyId": "company", "goalId": GOAL_ID, "pendingObservations": 0,
    "metrics": {"model_calls": metric(4, "admitted_calls"), "tokens": metric(1700, "provider_reported"),
                "model_usd": metric(0.42, "configured_token_rate_upper_bound"), "builds": metric(2, "isolated_verifier_admissions")},
    "points": {"spent": 12, "balance": 30},
    "receipts": [{"operationId": GOAL_ID, "sourceSha": SHA, "head": SHA, "builds": 2, "costUsd": 0.42}],
}
FOUNDER_USAGE = {
    "version": 1, "history": "retained_recorded_usage", "coverage": "observed_goal_meter_runs",
    "observedAt": "2026-10-30T00:00:00+00:00", "recordedRuns": 1, "incompleteRuns": 0, "truncated": False,
    "coverageIncomplete": False, "companyId": "company", "pendingObservations": 0,
    "metrics": {"model_calls": metric(4, "admitted_calls"), "tokens": metric(1700, "provider_reported"),
                "builds": metric(2, "isolated_verifier_admissions")},
    "points": {"spent": 12, "balance": 30},
    "receipts": [{"builds": 2}],
}


@override_settings(ARTICLES_COST_ACCOUNTING_ENABLED=True)
class FounderCostUsageTests(TestCase):
    def read_usage(self, built, *, staff=False):
        built.owner.is_staff = staff
        built.owner.save(update_fields=["is_staff"])
        client = APIClient()
        client.force_authenticate(user=built.owner)
        bodies = []
        for url in URLS:
            with patch.object(ledger, "read", return_value=deepcopy(USAGE)):
                response = client.get(url, {"company_id": str(built.company.pk)})
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response["Cache-Control"], "private, no-store")
            bodies.append(response.data["usage"])
        self.assertEqual(bodies[0], bodies[1])
        return bodies[0]

    def test_strip_keeps_counts_and_points_only(self):
        self.assertEqual(founder_cost_usage(deepcopy(USAGE)), FOUNDER_USAGE)

    @override_settings(**ALL_P1_FLAGS_ON)
    def test_a_new_journey_founder_sees_no_usd_sha_or_operation_id(self):
        usage = self.read_usage(make_new_journey_org())
        self.assertEqual(usage, FOUNDER_USAGE)
        text = str(usage)
        for hidden in (GOAL_ID, SHA, "usd", "Usd"):
            self.assertNotIn(hidden, text)

    @override_settings(**ALL_P1_FLAGS_ON)
    def test_staff_see_the_full_payload(self):
        self.assertEqual(self.read_usage(make_new_journey_org(), staff=True), USAGE)

    def test_legacy_orgs_and_v2_off_see_todays_payload(self):
        new = make_new_journey_org()
        with override_settings(**WITHOUT_V2):
            self.assertEqual(self.read_usage(new), USAGE)
        for name in NAMED_LEGACY_ORGS:
            built = make_named_legacy_org(name, marker=name != "mlai")
            for flags in (ALL_P1_FLAGS_ON, WITHOUT_V2):
                with self.subTest(name=name, v2=flags["MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED"]), override_settings(**flags):
                    self.assertEqual(self.read_usage(built), USAGE)
