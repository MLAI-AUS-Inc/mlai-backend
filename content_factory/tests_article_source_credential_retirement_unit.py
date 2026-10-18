"""Actual source-stop prepare -> durable broker discovery -> provider retirement."""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import tests_website_article_source_stop_unit as fixtures
from . import website_article_source_stop as stop, website_article_credentials as inventory, website_tokens as broker
from .website_contract import WebsiteAuthorityError, evidence_digest


@override_settings(**fixtures.ArticleSourceStopTests._overridden_settings)
class ArticleSourceCredentialRetirementTests(SimpleTestCase):
    def setUp(self):
        self.fixture = fixtures.ArticleSourceStopTests("runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.parent, self.config, self.now = self.fixture.parent, self.fixture.config, self.fixture.now
        self.connection = self.parent.connection
        self.original = self.fixture.fixture.workflow
        self.saved = deepcopy(self.fixture.fixture.root.run_request)
        # Synthetic pre-stop enrollment, as if retained from original birth.
        self.original.payload = {**self.original.payload, "attempt": 1, "deletion_epoch": 0,
            "first_article_goal_operation_id": str(self.parent.pk),
            "article_delivery_request": {**self.saved, "article_delivery_v1": {"schema_version": 1}}}
        self.channel = self.seed_legacy_channel()
        self.fixture.token_delete.side_effect = self.retire
        self.fixture.stack.enter_context(patch("content_factory.website_article_source_history.advance", return_value=None))

    def seed_legacy_channel(self):
        """Retained v1 history predates source fencing and cannot be retrofitted."""
        scope = {"schema_version": 1, "channel": "website_token_broker", "workflow_operation_id": str(self.original.pk),
            "goal_id": str(self.parent.pk), "connection_id": str(self.connection.pk),
            "connection_generation": self.connection.generation, "repository_id": str(self.connection.repository_id),
            "deletion_epoch": 0, "source_sha": self.saved["expected_source_sha"], "client_request_id": self.saved["client_request_id"]}
        row, fresh = self.fixture.fixture.reserve(idempotency_key=f"{self.connection.pk}:credential-inventory:{self.original.pk}",
            defaults={"action": inventory.INVENTORY_ACTION, "state": "recorded", "payload": scope,
                "receipt": {"schema_version": 1, "from_operation_start": True, "token_count": 0}})
        self.assertTrue(fresh)
        self.original.payload[inventory.MARKER] = {"schema_version": 1, "inventory_operation_id": str(row.pk), "scope_digest": evidence_digest(scope)}
        self.fixture.credential_rows.rows.append(row)
        return row

    def seed_token(self, *, bearer=True):
        with inventory.operation_scope(self.original):
            reservation = inventory.reserve(self.connection, {**self.saved, "operation_id": str(self.original.pk),
                "operation_attempt": 1, "run_id": self.fixture.fixture.root.run_id}, mode="write", action="publish", profile="repository")
        inventory.issued(reservation, SimpleNamespace(expires_at=timezone.now() + timedelta(minutes=30)))
        inventory.delivery_possible(reservation)
        if bearer:
            self.fixture.cached[f"website-issued-token:{reservation['id']}"] = "synthetic-retirement-token-canary"
        return next(row for row in self.fixture.credential_rows.rows if str(row.pk) == reservation["id"])

    def retire(self, *args, **kwargs):
        self.assertEqual(self.fixture.fixture.fixture.depth, 0, "No site lock across provider DELETE")
        self.assertIn("article_source_stop", self.parent.receipt, "Local execution stops before retirement")
        return SimpleNamespace(status_code=204)

    def receipt(self):
        operation = stop._operation(self.parent.receipt[stop.KEY])
        return operation.receipt.get("broker_credential_retirement")

    def advance(self, *, now=None):
        return stop.advance(self.parent, self.config, now=now or self.now)

    def test_actual_prepare_retires_durable_token_without_cache_index_and_no_new_charge(self):
        from .website_prepare import _advance_claimed_prepare
        token = self.seed_token()
        with patch("content_factory.website_first_article_billing.reservation_ready", return_value=True), \
                patch("content_factory.website_prepare.journey_for_context", side_effect=AssertionError("No replacement journey")), \
                patch("content_factory.website_prepare._finish_prepare", return_value=self.parent):
            _advance_claimed_prepare(self.parent, now=self.now, handlers=None)
        self.fixture.token_delete.assert_called_once()
        self.assertEqual(token.receipt["revocation_state"], "confirmed")
        self.assertEqual(self.receipt()["operations"], [{"operation_id": str(self.original.pk), "revoked": 1, "pending": 0}])
        self.assertFalse(self.receipt()["quiet"]); self.assertFalse(self.receipt()["inventory_complete"])
        self.assertFalse(self.receipt()["effect_authority"])
        self.assertNotIn("synthetic-retirement-token-canary", json.dumps(self.receipt()))
        self.assertEqual(self.fixture.fixture.fixture.chain.fixture.spend.call_count, 1)

    def test_missing_bearer_stays_pending_then_uses_only_actual_expiry(self):
        token = self.seed_token(bearer=False)
        self.advance()
        self.assertEqual(self.receipt()["operations"][0]["pending"], 1)
        self.fixture.token_delete.assert_not_called()
        with patch.object(inventory.timezone, "now", return_value=timezone.now() + timedelta(hours=1)):
            self.advance(now=self.now + timedelta(seconds=121))
        self.assertEqual(self.receipt()["operations"][0]["pending"], 0)
        self.assertEqual(token.state, "completed")
        self.assertFalse(self.receipt()["quiet"])
        self.assertEqual(self.fixture.rpc.call_count, 1)

    def test_bounded_passes_drain_original_references_without_replaying_worker_stop(self):
        tokens = [self.seed_token() for _ in range(5)]
        self.advance()
        self.assertEqual(self.fixture.token_delete.call_count, 1)
        self.assertEqual(self.receipt()["operations"][0]["pending"], 4)
        for index in range(1, 5):
            self.advance(now=self.now + timedelta(seconds=121 * index))
        self.assertEqual(self.fixture.token_delete.call_count, 5)
        self.assertTrue(all(row.receipt["revocation_state"] == "confirmed" for row in tokens))
        self.assertEqual(self.fixture.rpc.call_count, 1)
        self.assertFalse(self.receipt()["quiet"])

    def test_missing_bearers_do_not_starve_other_known_credentials_of_retirement(self):
        tokens = sorted([self.seed_token() for _ in range(3)], key=lambda row: str(row.pk))
        for row in tokens[:2]:
            self.fixture.cached.pop(f"website-issued-token:{row.pk}")
        self.advance()
        self.fixture.token_delete.assert_called_once()
        self.assertEqual(tokens[-1].receipt["revocation_state"], "confirmed")
        self.assertEqual(self.receipt()["operations"][0]["pending"], 2)

    def test_owner_revocation_after_delete_retains_token_observation_but_no_control_receipt(self):
        token = self.seed_token()
        def revoked(*args, **kwargs):
            response = self.retire(*args, **kwargs)
            self.parent.state = "cancelled"
            return response
        self.fixture.token_delete.side_effect = revoked
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.assertEqual(token.receipt["revocation_state"], "confirmed")
        self.assertIsNone(self.receipt())
        self.assertFalse(self.parent.receipt["article_source_stop"]["quiet"])

    def test_uncertain_provider_delete_is_durable_and_lost_lease_reply_retries(self):
        token = self.seed_token()
        self.fixture.token_delete.side_effect = RuntimeError("synthetic-private-provider-body-canary")
        self.advance()
        self.assertEqual(token.receipt["revocation_state"], "uncertain")
        self.assertEqual(self.receipt()["operations"][0]["pending"], 1)
        self.assertNotIn("synthetic-private-provider-body-canary", json.dumps(self.receipt()))
        operation = stop._operation(self.parent.receipt[stop.KEY])
        operation.receipt["credential_lease"] = {"nonce": "11111111-1111-4111-8111-111111111111",
            "request_digest": operation.payload["request"]["digest"], "expires_at": (self.now + timedelta(seconds=120)).isoformat()}
        self.advance(now=self.now + timedelta(seconds=30))
        self.assertEqual(self.fixture.token_delete.call_count, 1)
        self.fixture.token_delete.side_effect = self.retire
        self.advance(now=self.now + timedelta(seconds=121))
        self.assertEqual(token.receipt["revocation_state"], "confirmed")
        self.assertEqual(self.fixture.rpc.call_count, 1)

    def test_refund_changed_before_retirement_permits_no_provider_work(self):
        self.seed_token()
        self.advance()
        calls = self.fixture.token_delete.call_count
        with patch("content_factory.website_first_article_billing.for_generation", side_effect=WebsiteAuthorityError("website_operation_changed", "Original refund changed")):
            with self.assertRaises(WebsiteAuthorityError): self.advance(now=self.now + timedelta(seconds=121))
        self.assertEqual(self.fixture.token_delete.call_count, calls)

    def test_invalid_lease_cannot_discover_or_retire_more_credentials(self):
        self.seed_token()
        self.advance()
        operation = stop._operation(self.parent.receipt[stop.KEY])
        operation.receipt["credential_lease"] = {"nonce": "invalid"}
        with self.assertRaises(WebsiteAuthorityError): self.advance(now=self.now + timedelta(seconds=121))
        self.assertEqual(self.fixture.token_delete.call_count, 1)

    def test_initial_effect_removal_includes_credentials_after_goal_compute_budget(self):
        token = self.seed_token()
        self.advance(now=self.now + timedelta(hours=49))
        self.assertEqual(token.receipt["revocation_state"], "confirmed")
        self.assertEqual(self.parent.state, "pending")
        self.assertIsNotNone(self.parent.next_attempt_at)
        self.assertEqual(self.parent.receipt["status"], "stopped")
        self.assertEqual(self.parent.receipt["budget_stop_reason"], "wall_time")
        from .website_first_article import active
        self.assertFalse(active(self.parent))
        self.assertEqual(self.fixture.fixture.fixture.chain.fixture.spend.call_count, 1)
        self.assertFalse(self.receipt()["quiet"])

    def test_same_goal_and_source_do_not_authorize_retiring_a_distinct_execution(self):
        captured = self.seed_token()
        original = self.original
        self.original = SimpleNamespace(**{**vars(original), "pk": uuid4(), "payload": deepcopy(original.payload)})
        self.original.payload.pop(inventory.MARKER)
        self.seed_legacy_channel()
        separate = self.seed_token()
        self.original = original
        self.advance()
        self.assertEqual(captured.receipt["revocation_state"], "confirmed")
        self.assertEqual(separate.receipt["revocation_state"], "not_attempted")
        self.assertIn(f"website-issued-token:{separate.pk}", self.fixture.cached)
        self.fixture.token_delete.assert_called_once()
        self.assertFalse(self.receipt()["inventory_complete"])

    def test_changed_retirement_lease_after_delete_cannot_commit_control_receipt(self):
        token = self.seed_token()
        def changed(*args, **kwargs):
            response = self.retire(*args, **kwargs)
            operation = stop._operation(self.parent.receipt[stop.KEY])
            operation.receipt["credential_lease"]["nonce"] = str(uuid4())
            return response
        self.fixture.token_delete.side_effect = changed
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.assertEqual(token.receipt["revocation_state"], "confirmed")
        self.assertIsNone(self.receipt())
