"""Exact server scope at birth and actual source-stop retirement of deeper work."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import tests_article_credential_inventory_unit as broker_fixtures
from . import tests_article_source_credential_retirement_unit as stop_fixtures
from . import website_article_credentials as inventory, website_article_source_fence as fences
from . import website_article_source_stop as stops, website_goal as goals, website_setup_consent as consent
from .website_connections import contract_for
from .website_contract import WebsiteAuthorityError, evidence_digest
from .website_operations import reserve_workflow_operation


@override_settings(**broker_fixtures.ArticleCredentialInventoryTests._overridden_settings)
class ArticleCredentialSourceScopeTests(SimpleTestCase):
    def setUp(self):
        self.fixture = broker_fixtures.ArticleCredentialInventoryTests("runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def reserve(self, key="c", **changes):
        f = self.fixture
        request = {**contract_for(f.connection), "client_request_id": "articles-first:" + key * 64,
            "expected_source_sha": "a" * 40, "first_article_goal_id": str(f.parent.pk),
            "first_article_goal_operation_id": str(f.parent.pk), **changes}
        operation = reserve_workflow_operation(f.connection, workflow="direct_generate", payload=request)
        channel = next(row for row in f.operations.rows if str(row.pk) == operation.payload[inventory.MARKER]["inventory_operation_id"])
        return operation, channel

    def fence(self):
        f = self.fixture
        return {"binding": {"goal": {"goal_id": str(f.parent.pk)}, "source_scope": deepcopy(f.parent.receipt["goal"]["scope"]),
            "source_sha": "a" * 40, "runs": [{"operation_id": str(f.operation.pk)}]}}

    def test_actual_workflow_birth_records_server_scope_instead_of_copied_request_budget(self):
        f = self.fixture
        _, row = self.reserve(goal_budget={"scope_digest": "f" * 64}, source_scope={"configuration_revision": 999})
        self.assertEqual(row.payload["schema_version"], 2)
        self.assertEqual(row.payload["source_scope"], f.parent.receipt["goal"]["scope"])
        self.assertEqual(row.payload["source_scope_digest"], goals.digest(f.parent.receipt["goal"]["scope"]))
        self.assertEqual(row.payload["source_scope"]["configuration_revision"], 1)
        self.assertEqual(row.receipt["token_count"], 0)
        with self.assertRaises(WebsiteAuthorityError): self.reserve(key="d", configuration_revision=999)
        f.mint.assert_not_called()

    def test_source_fenced_parent_cannot_create_a_deeper_channel(self):
        f = self.fixture
        f.parent.receipt[fences.KEY] = str(uuid4())
        with self.assertRaises(WebsiteAuthorityError): self.reserve()
        self.assertEqual(len([row for row in f.operations.rows if row.action == inventory.INVENTORY_ACTION]), 1)
        f.mint.assert_not_called()

    def test_retained_v1_channel_is_never_retrofitted_into_known_source_coverage(self):
        f = self.fixture
        row = f.channel()
        row.payload = {key: value for key, value in row.payload.items() if key not in {"source_scope", "source_scope_digest"}}
        row.payload["schema_version"] = 1
        f.operation.payload[inventory.MARKER]["scope_digest"] = evidence_digest(row.payload)
        before = deepcopy(row.payload)
        with self.settings(ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=False): f.issue()
        self.assertFalse(inventory.enroll(f.operation, f.request, f.connection, created=False))
        self.assertEqual(row.payload, before)
        self.assertEqual(row.receipt["token_count"], 1)
        self.assertEqual(inventory.source_page(f.connection.pk, self.fence()), ([], None))

    def test_pages_select_original_configuration_and_exclude_later_same_sha_execution(self):
        f = self.fixture
        _, old = self.reserve()
        original = self.fence()
        f.connection.configuration_version = 3
        goal = f.parent.receipt["goal"]
        goal["scope"] = goals.scope_for(f.connection, "9")
        goal["consent"] = consent.seal(goal, goal["consent"]["answers"], user_id=4, now=timezone.now())
        _, later = self.reserve(key="d")
        selected, cursor = inventory.source_page(f.connection.pk, original)
        self.assertEqual(selected, [old.payload["workflow_operation_id"]])
        self.assertNotIn(later.payload["workflow_operation_id"], selected)
        self.assertIsNone(cursor)

    def test_changed_current_configuration_refuses_original_v2_token_mint(self):
        f = self.fixture
        f.connection.configuration_version += 1
        with self.assertRaises(WebsiteAuthorityError): f.issue()
        f.mint.assert_not_called()

    def test_scope_corruption_refuses_mint_and_foreign_enumeration_before_provider(self):
        f = self.fixture
        f.channel().payload["source_scope"]["repository_id"] = "999"
        with self.assertRaises(WebsiteAuthorityError): f.issue()
        with self.assertRaises(WebsiteAuthorityError): inventory.channel_scope(f.channel())
        with self.assertRaises(WebsiteAuthorityError): inventory.source_page(uuid4(), self.fence())
        f.mint.assert_not_called()

    def test_uncertain_token_retries_do_not_starve_other_bearers_in_same_operation(self):
        f = self.fixture
        for index in range(3):
            f.credential = type(f.credential)(token=f"synthetic-scoped-token-{index}", expires_at=timezone.now() + timedelta(minutes=30),
                installation_id="4", repository="fixture/site")
            f.issue()
        def retire(url, **kwargs):
            response = f.retire(url, **kwargs)
            return SimpleNamespace(status_code=404) if kwargs["headers"]["Authorization"] == "Bearer synthetic-scoped-token-0" else response
        f.delete.side_effect = retire
        from .website_tokens import revoke_generation_tokens
        results = [revoke_generation_tokens(f.connection.pk, f"operation:{f.operation.pk}", maximum_calls=1) for _ in range(3)]
        self.assertEqual([result["revoked"] for result in results], [0, 1, 1])
        self.assertEqual(f.delete.call_count, 3)
        self.assertEqual(f.tokens()[0].receipt["revocation_state"], "uncertain")
        self.assertTrue(all(row.receipt["revocation_state"] == "confirmed" for row in f.tokens()[1:]))


@override_settings(**stop_fixtures.ArticleSourceCredentialRetirementTests._overridden_settings)
class ArticleScopedCredentialRetirementTests(SimpleTestCase):
    def setUp(self):
        self.fixture = stop_fixtures.ArticleSourceCredentialRetirementTests("runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def retained_v2(self, *, revision=1):
        """Seed a retained v2 snapshot; real birth is exercised above."""
        f = self.fixture
        original = f.original
        f.original = SimpleNamespace(**{**vars(original), "pk": uuid4(), "payload": deepcopy(original.payload)})
        f.original.payload.pop(inventory.MARKER)
        row = f.seed_legacy_channel()
        token = f.seed_token()
        scope = deepcopy(f.fixture.fence.payload["binding"]["source_scope"])
        scope["configuration_revision"] = revision
        row.payload = {**row.payload, "schema_version": 2, "source_scope": scope, "source_scope_digest": goals.digest(scope)}
        digest = evidence_digest(row.payload)
        token.payload["scope_digest"] = digest
        f.original.payload[inventory.MARKER]["scope_digest"] = digest
        f.original = original
        return row, token

    def test_actual_prepare_retires_unlisted_original_scoped_child_but_preserves_later_reversion(self):
        f = self.fixture
        old, token = self.retained_v2()
        later, separate = self.retained_v2(revision=3)
        f.advance()
        f.fixture.token_delete.assert_called_once()
        self.assertEqual(token.receipt["revocation_state"], "confirmed")
        self.assertEqual(separate.receipt["revocation_state"], "not_attempted")
        receipt = f.receipt()
        self.assertIn(old.payload["workflow_operation_id"], [row["operation_id"] for row in receipt["operations"]])
        self.assertNotIn(later.payload["workflow_operation_id"], [row["operation_id"] for row in receipt["operations"]])
        self.assertEqual(receipt["scope"], "captured_and_source_scoped_operations")
        self.assertFalse(receipt["quiet"]); self.assertFalse(receipt["inventory_complete"])

    def test_source_pages_advance_and_wrap_without_starving_deeper_operations(self):
        f = self.fixture
        children = [self.retained_v2() for _ in range(5)]
        for index in range(3):
            f.advance(now=f.now + timedelta(seconds=121 * index))
        self.assertTrue(all(token.receipt["revocation_state"] == "confirmed" for _, token in children))
        self.assertEqual(f.fixture.token_delete.call_count, 5)
        self.assertIsNone(f.receipt()["next_inventory_id"])
        self.assertEqual(f.fixture.rpc.call_count, 1)
        self.assertFalse(f.receipt()["quiet"])

    def test_provider_uncertainty_in_captured_operation_does_not_starve_deeper_channels(self):
        f = self.fixture
        original = f.seed_token()
        _, deeper = self.retained_v2()
        def uncertain(url, **kw):
            response = f.retire(url, **kw)
            return SimpleNamespace(status_code=404) if original.receipt["revocation_attempts"] == 0 else response
        f.fixture.token_delete.side_effect = uncertain
        f.advance()
        self.assertEqual(f.fixture.token_delete.call_count, 2)
        self.assertEqual(deeper.receipt["revocation_attempts"], 1)
        self.assertFalse(f.receipt()["quiet"])

    def test_changed_source_page_cursor_refuses_provider_work(self):
        f = self.fixture
        self.retained_v2()
        f.advance()
        f.receipt()["next_inventory_id"] = "invalid-private-cursor"
        calls = f.fixture.token_delete.call_count
        with self.assertRaises(WebsiteAuthorityError): f.advance(now=f.now + timedelta(seconds=121))
        self.assertEqual(f.fixture.token_delete.call_count, calls)

    def test_legacy_stop_receipt_upgrades_without_inventing_legacy_channel_coverage(self):
        f = self.fixture
        f.advance()
        receipt = f.receipt()
        receipt["schema_version"] = 1
        receipt["scope"] = "captured_original_operations"
        receipt.pop("next_inventory_id")
        self.retained_v2()
        f.advance(now=f.now + timedelta(seconds=121))
        self.assertEqual(f.receipt()["schema_version"], 2)
        self.assertFalse(f.receipt()["inventory_complete"])
