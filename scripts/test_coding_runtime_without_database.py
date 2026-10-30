"""Exercise the Coding entitlement without project settings or a database.

Uses unittest directly: no Django test runner, database creation, or migrations.
Run with the backend's Python 3.11 dependencies installed.
"""

import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from django.conf import settings

settings.configure(
    SECRET_KEY="coding-runtime-unit-tests-only",
    INSTALLED_APPS=[
        "django.contrib.auth",
        "django.contrib.contenttypes",
        "core",
        "organizations",
        "workflow_runs",
        "content_factory",
        "roo",
        "community_chat",
    ],
    AUTH_USER_MODEL="core.User",
    DATABASES={"default": {"ENGINE": "django.db.backends.dummy"}},
    USE_TZ=True,
    DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
    REST_FRAMEWORK={"DEFAULT_THROTTLE_CLASSES": []},
)

import django

django.setup()

from community_chat import coding_views


class CodingRuntimeContractTests(unittest.TestCase):
    """Verify the additive runtime field preserves the surrounding contract."""

    def entitlement(
        self, *, eligible=True, balance=1_000_000, active=None,
        digital_balance=None, reserved=0,
    ):
        request = SimpleNamespace(user=object())
        pricing = {"version": "existing-tariff"}
        # This patch is also checked against the current deployment's broader
        # eligibility policy. Neither policy is replaced by the runtime change.
        eligibility_name = (
            "user_can_use_coding"
            if hasattr(coding_views, "user_can_use_coding")
            else "user_has_pilot_access"
        )
        with ExitStack() as stack:
            reconcile = stack.enter_context(
                patch.object(coding_views, "reconcile_coding_reservations")
            )
            stack.enter_context(patch.object(coding_views, "current_pricing"))
            stack.enter_context(
                patch.object(coding_views, eligibility_name, return_value=eligible)
            )
            stack.enter_context(
                patch.object(
                    coding_views.PointsService,
                    "get_balance",
                    return_value={
                        "balance_microroo": balance,
                        "digital_service_balance_microroo": (
                            balance if digital_balance is None else digital_balance
                        ),
                    },
                )
            )
            reservation = stack.enter_context(
                patch.object(
                    coding_views.PointsService, "_reserved_microroo",
                    return_value=reserved,
                )
            )
            stack.enter_context(
                patch.object(coding_views, "pricing_payload", return_value=pricing)
            )
            stack.enter_context(
                patch.object(coding_views, "_turn_payload", return_value=active)
            )
            manager = stack.enter_context(patch.object(coding_views.CodingTurn, "objects"))
            manager.filter.return_value.select_related.return_value.first.return_value = (
                SimpleNamespace(pk=active["id"]) if active else None
            )
            response = coding_views.CodingEntitlementView().get(request)
            reconcile.assert_called_once_with(user=request.user)
            self.assertEqual(manager.filter.call_args.kwargs["user"], request.user)
            reservation.assert_called_once_with(
                request.user, exclude_turn_id=active["id"] if active else None,
                include_digital=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["model"], "kimi-k3")
        self.assertEqual(response.data["pricing"], pricing)
        self.assertNotIn("models", response.data)
        self.assertNotIn("client_protocol_version", response.data)
        return response.data

    def test_old_and_new_clients_can_negotiate_their_bundled_runtime(self):
        entitlement = self.entitlement()
        runtime = entitlement["runtime"]
        self.assertEqual(runtime["kimi_code_version"], "0.36.1")
        for bundled_version in ("0.36.1", "0.39.1", "2.3.1"):
            self.assertIn(bundled_version, runtime["compatible_kimi_code_versions"])
        self.assertTrue(runtime["desktop_only"])
        self.assertEqual(runtime["node_major"], 24)
        self.assertTrue(entitlement["can_start_turn"])

    def test_existing_account_balance_and_active_turn_gates_are_preserved(self):
        for options in (
            {"eligible": False},
            {"balance": 0},
            {"active": {"id": "existing-turn"}},
        ):
            with self.subTest(options=options):
                entitlement = self.entitlement(**options)
                self.assertFalse(entitlement["can_start_turn"])
                self.assertEqual(entitlement["pilot_access"], options.get("eligible", True))
                self.assertEqual(entitlement["active_turn"], options.get("active"))

    def test_digital_service_balance_and_reservations_remain_authoritative(self):
        restricted_only = self.entitlement(balance=10_000_000, digital_balance=0)
        fully_reserved = self.entitlement(balance=1_000_000, reserved=1_000_000)
        for entitlement in (restricted_only, fully_reserved):
            self.assertFalse(entitlement["can_start_turn"])
            self.assertEqual(entitlement["balance_microroo"], "0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
