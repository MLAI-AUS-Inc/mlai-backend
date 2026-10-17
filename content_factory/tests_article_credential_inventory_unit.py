"""Real guard -> workflow reservation -> broker -> durable revoke with ORM/provider fakes."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import website_article_credentials as inventory, website_connections as authority, website_tokens as broker
from . import website_goal as goals, website_setup_consent as consent
from .website_operations import reserve_workflow_operation, validate_operation
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnection, WebsiteConnectionOperation
from .tests_website_setup_consent_unit import answers
from integrations.services.github_app import GitHubInstallationToken


class Rows:
    def __init__(self, items):
        self.items = list(items)
    def filter(self, **fields):
        def match(row):
            for key, expected in fields.items():
                value = row
                lookup = key.split("__")
                operator = lookup[-1] if lookup[-1] in {"in", "gt"} else None
                for part in lookup[:-1] if operator else lookup:
                    value = value.get(part) if isinstance(value, dict) else getattr(value, part, None)
                if operator == "in":
                    if value not in expected: return False
                elif operator == "gt":
                    if str(value) <= str(expected): return False
                elif str(value) != str(expected): return False
            return True
        return Rows(row for row in self.items if match(row))
    def exclude(self, **fields):
        rejected = self.filter(**fields).items
        return Rows(row for row in self.items if row not in rejected)
    def first(self): return self.items[0] if self.items else None
    def get(self, **fields): return self.filter(**fields).items[0]
    def exists(self): return bool(self.items)
    def select_related(self, *a): return self
    def select_for_update(self): return self
    def order_by(self, *fields):
        result = list(self.items)
        for field in reversed(fields):
            result.sort(key=lambda row: str(getattr(row, field.removeprefix("-"), "")), reverse=field.startswith("-"))
        return Rows(result)
    def __getitem__(self, key): return self.items[key]


class Operations:
    def __init__(self): self.rows = []
    def filter(self, **fields): return Rows(self.rows).filter(**fields)
    def select_related(self, *a): return self
    def select_for_update(self): return self
    def exclude(self, **fields): return Rows(self.rows).exclude(**fields)
    def create(self, **fields):
        row = SimpleNamespace(**{"pk": UUID(str(fields.pop("pk"))) if "pk" in fields else uuid4(), "receipt": {}, "payload": {}, **fields})
        row.connection_id = row.connection.pk
        row.created_at = len(self.rows)
        row.save = Mock()
        self.rows.append(row)
        return row
    def get_or_create(self, *, idempotency_key, defaults):
        existing = next((r for r in self.rows if r.idempotency_key == idempotency_key), None)
        return (existing, False) if existing else (self.create(idempotency_key=idempotency_key, **{key: deepcopy(value) if key in {"payload", "receipt"} else value for key, value in defaults.items()}), True)


@override_settings(ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=True, ARTICLES_SETUP_CONSENT_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True, ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True, ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED=True, ARTICLES_ARTICLE_RECOVERY_ENABLED=True,
    ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=True, CONTENT_FACTORY_ARTICLE_DELIVERY_V1_APPLY_ENABLED=True)
class ArticleCredentialInventoryTests(SimpleTestCase):
    def setUp(self):
        self.depth = 0
        self.organization = SimpleNamespace(pk=7, id=7, domain="fixture.test")
        identifier = uuid4()
        self.connection = SimpleNamespace(pk=identifier, id=identifier, generation=3, repository_id=42,
            installation_id="4", github_repo="fixture/site", branch="main", app_root="", blockers=[],
            capabilities={}, state="connected", organization=self.organization, organization_id=7,
            configuration_version=1, authorized_by_id=4, verified_sha="a" * 40)
        self.operations = Operations()
        self.connection.operations = self.operations
        self.connection.scan_snapshots = Rows([])
        self.connection.targets = Rows([])
        goal_id = uuid4()
        goal = goals.new_goal(goal_id, goals.scope_for(self.connection, "9"), target="first_article_live", now=timezone.now())
        goal["current_source_sha"] = "a" * 40
        goal["consent"] = consent.seal(goal, answers(), user_id=4, now=timezone.now())
        self.parent = self.operations.create(pk=goal_id, connection=self.connection, generation=3, action="prepare",
            state="running", idempotency_key="prepare", payload={"company_id": "9"}, receipt={"goal": goal})
        @contextmanager
        def atomic():
            self.depth += 1
            try: yield
            finally: self.depth -= 1
        self.start(patch("django.db.transaction.atomic", side_effect=atomic))
        self.start(patch.object(WebsiteConnection.objects, "select_related", side_effect=lambda *a: Rows([self.connection])))
        self.start(patch.object(WebsiteConnection.objects, "select_for_update", side_effect=lambda: Rows([self.connection])))
        self.start(patch("organizations.models.Organization.objects.select_for_update", side_effect=lambda: Rows([self.organization])))
        self.start(patch("content_factory.models.OrganizationContentConfig.objects.filter", return_value=Rows([SimpleNamespace()])))
        self.start(patch("workflow_runs.models.ContentFactoryRun.objects.filter", return_value=Rows([])))
        for name in ("select_related", "select_for_update", "filter", "get_or_create", "create"):
            self.start(patch.object(WebsiteConnectionOperation.objects, name, side_effect=getattr(self.operations, name)))
        self.cached = {}
        self.start(patch.object(broker.cache, "get", side_effect=lambda key: self.cached.get(key)))
        self.cache_set = self.start(patch.object(broker.cache, "set", side_effect=lambda key, value, **kw: self.cached.__setitem__(key, value)))
        self.start(patch.object(broker.cache, "delete", side_effect=lambda key: self.cached.pop(key, None)))
        self.credential = GitHubInstallationToken(token="synthetic-token-canary", expires_at=timezone.now() + timedelta(minutes=30),
            installation_id="4", repository="fixture/site")
        self.mint = self.start(patch("integrations.services.github_app.create_installation_access_token", side_effect=self.provider))
        self.delete = self.start(patch("integrations.http_client.delete", side_effect=self.retire))
        self.request = {**authority.contract_for(self.connection), "client_request_id": "articles-first:" + "b" * 64,
            "expected_source_sha": "a" * 40, "first_article_goal_id": str(goal_id), "first_article_goal_operation_id": str(goal_id)}
        self.operation = reserve_workflow_operation(self.connection, workflow="direct_generate", payload=self.request)
        self.data = {**self.request, "run_id": "original-article"}

    def start(self, patcher):
        result = patcher.start(); self.addCleanup(patcher.stop); return result

    def provider(self, **kw):
        self.assertEqual(self.depth, 0, "No connection/metadata lock during provider mint")
        self.assertEqual(authority._authority_depth.get(), 0)
        row = self.tokens()[-1]
        self.assertEqual(row.receipt["mint_state"], "pending")
        self.assertFalse(row.receipt["delivery_possible"])
        return self.credential

    def retire(self, *a, **kw):
        self.assertEqual(self.depth, 0, "No connection/metadata lock during provider revoke")
        self.assertEqual(authority._authority_depth.get(), 0)
        return SimpleNamespace(status_code=204)

    def tokens(self): return [r for r in self.operations.rows if r.action == inventory.TOKEN_ACTION]
    def channel(self): return next(r for r in self.operations.rows if r.action == inventory.INVENTORY_ACTION)
    def issue(self): return broker.mint_website_token(self.data)

    def test_actual_new_workflow_initializes_channel_before_provider_work(self):
        self.assertIn(inventory.MARKER, self.operation.payload)
        self.assertEqual(self.channel().receipt["token_count"], 0)
        self.mint.assert_not_called()
        result = self.issue()
        row = self.tokens()[0]
        self.assertEqual(result["credential_reference"], str(row.pk))
        self.assertEqual(row.receipt["expires_at"], self.credential.expires_at.isoformat())
        self.assertTrue(row.receipt["delivery_possible"])
        self.assertEqual(row.receipt["mint_state"], "issued")
        self.assertEqual(self.channel().receipt["token_count"], 1)
        self.assertNotIn("synthetic-token-canary", json.dumps([{ "payload": r.payload, "receipt": r.receipt} for r in self.operations.rows]))
        self.assertIsNone(inventory._current_operation.get())

    def test_flag_rollback_retains_original_inventory_and_missing_channel_cannot_reset_zero(self):
        with self.settings(ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=False): self.issue()
        self.assertEqual(self.channel().receipt["token_count"], 1)
        self.operations.rows.remove(self.channel())
        with self.settings(ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=False), self.assertRaises(WebsiteAuthorityError): self.issue()
        self.assertEqual(self.mint.call_count, 1)

    def test_uncertain_provider_result_keeps_pre_http_record(self):
        from integrations.http_client import RequestException
        self.mint.side_effect = RequestException("synthetic lost mint reply")
        with self.assertRaises(WebsiteAuthorityError): self.issue()
        receipt = self.tokens()[0].receipt
        self.assertEqual(receipt["mint_state"], "uncertain")
        self.assertIsNone(receipt["expires_at"]); self.assertFalse(receipt["delivery_possible"])
        self.assertFalse(inventory.settled({}, receipt))
        self.assertEqual(self.cached, {})

    def test_post_mint_owner_stop_withholds_delivery_and_records_confirmed_revoke(self):
        def revoked(**kw):
            result = self.provider(**kw)
            self.operation.state = "cancelled"
            return result
        self.mint.side_effect = revoked
        with self.assertRaises(WebsiteAuthorityError): self.issue()
        receipt = self.tokens()[0].receipt
        self.assertFalse(receipt["delivery_possible"])
        self.assertEqual(receipt["expires_at"], self.credential.expires_at.isoformat())
        self.assertEqual(receipt["revocation_state"], "confirmed")
        self.delete.assert_called_once(); self.assertEqual(self.cached, {})

    def test_missing_provider_expiry_is_never_delivered(self):
        self.credential = GitHubInstallationToken(token="synthetic-token-canary", expires_at=None, installation_id="4", repository="fixture/site")
        with self.assertRaises(WebsiteAuthorityError): self.issue()
        self.assertFalse(self.tokens()[0].receipt["delivery_possible"])
        self.assertEqual(self.tokens()[0].receipt["revocation_state"], "confirmed")

    def test_cache_index_loss_still_revokes_durably_known_bearer(self):
        value = self.issue(); reference = value["credential_reference"]
        self.cached.pop(broker.token_index_key(self.connection.pk, self.connection.generation))
        self.cached.pop(broker.token_index_key(self.connection.pk, f"operation:{self.operation.pk}"))
        result = broker.revoke_generation_tokens(self.connection.pk, f"operation:{self.operation.pk}")
        self.assertEqual(result, {"revoked": 1, "pending": 0})
        self.assertNotIn(f"website-issued-token:{reference}", self.cached)
        self.assertEqual(self.tokens()[0].receipt["revocation_state"], "confirmed")

    def test_lost_bearer_and_indices_stays_pending_until_actual_expiry(self):
        self.issue(); self.cached.clear()
        result = broker.revoke_generation_tokens(self.connection.pk, f"operation:{self.operation.pk}")
        self.assertEqual(result, {"revoked": 0, "pending": 1})
        self.delete.assert_not_called()
        now = self.credential.expires_at + timedelta(seconds=1)
        with patch.object(inventory.timezone, "now", return_value=now):
            result = broker.revoke_generation_tokens(self.connection.pk, f"operation:{self.operation.pk}")
        self.assertEqual(result, {"revoked": 0, "pending": 0})

    def test_unconfirmed_provider_revoke_never_becomes_a_confirmed_receipt(self):
        self.issue(); self.delete.side_effect = lambda *a, **kw: SimpleNamespace(status_code=404)
        result = broker.revoke_generation_tokens(self.connection.pk, f"operation:{self.operation.pk}")
        self.assertEqual(result["pending"], 1)
        self.assertEqual(self.tokens()[0].receipt["revocation_state"], "uncertain")

    def test_unrelated_workflow_receipt_updates_do_not_erase_credential_inventory(self):
        self.issue()
        self.operation.receipt = {"status": "running"}
        self.assertEqual(self.channel().receipt["token_count"], 1)
        self.assertEqual(len(self.tokens()), 1)

    def test_private_inventory_rows_never_authorize_generic_credentials(self):
        self.issue()
        for row in (self.channel(), self.tokens()[0]):
            with self.assertRaises(WebsiteAuthorityError):
                validate_operation(self.connection, {**self.data, "operation_id": str(row.pk)}, cancellation=True)

    def test_nested_guard_without_operation_does_not_borrow_outer_credential_scope(self):
        with inventory.operation_scope(self.operation):
            with authority.authority_guard(authority.contract_for(self.connection)):
                self.assertIsNone(inventory._current_operation.get())
            self.assertIs(inventory._current_operation.get(), self.operation)
        self.assertIsNone(inventory._current_operation.get())

    def test_feature_off_original_cannot_be_retrofitted_after_rollout(self):
        request = {**authority.contract_for(self.connection), "client_request_id": "articles-first:" + "c" * 64,
            "expected_source_sha": "a" * 40, "first_article_goal_id": str(self.parent.pk), "first_article_goal_operation_id": str(self.parent.pk)}
        with self.settings(ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=False):
            operation = reserve_workflow_operation(self.connection, workflow="direct_generate", payload=request)
        self.assertNotIn(inventory.MARKER, operation.payload)
        reserve_workflow_operation(self.connection, workflow="direct_generate", payload=request)
        self.assertNotIn(inventory.MARKER, operation.payload)
        self.assertEqual(len([r for r in self.operations.rows if r.action == inventory.INVENTORY_ACTION]), 1)
        self.mint.side_effect = lambda **kw: self.credential
        broker.mint_website_token({**request, "run_id": "legacy-article"})
        self.assertEqual(self.tokens(), [])

    def test_corrupt_channel_cannot_admit_or_create_a_new_zero_count(self):
        self.channel().receipt["token_count"] = True
        with self.assertRaises(WebsiteAuthorityError): self.issue()
        self.mint.assert_not_called()
        self.assertEqual(self.tokens(), [])

    def test_expired_credential_before_delivery_is_retired_without_exposure(self):
        real = inventory.issued
        def expired(*a, **kw):
            real(*a, **kw)
            self.tokens()[0].receipt["expires_at"] = (timezone.now() - timedelta(seconds=1)).isoformat()
        with patch.object(inventory, "issued", side_effect=expired), self.assertRaises(WebsiteAuthorityError): self.issue()
        self.assertFalse(self.tokens()[0].receipt["delivery_possible"])
        self.assertEqual(self.tokens()[0].receipt["revocation_state"], "confirmed")

    def test_confirmed_revocation_is_not_downgraded_by_a_late_failure(self):
        self.issue()
        discovered, _ = inventory.discover(self.connection.pk, f"operation:{self.operation.pk}")
        reservation = next(iter(discovered.values()))[0]
        inventory.revocation(reservation, confirmed=True)
        inventory.revocation(reservation, confirmed=False)
        self.assertEqual(self.tokens()[0].receipt["revocation_state"], "confirmed")

    def test_private_records_do_not_replace_the_public_latest_workflow(self):
        self.issue()
        self.connection.repository_mutations = Rows([])
        self.connection.site_url = "fixture.test"
        config = SimpleNamespace(website_connection=self.connection, default_publish_target_id=None, last_scanned_sha="")
        projection = authority.summary_for(config)
        self.assertEqual(projection["latestOperation"]["id"], str(self.operation.pk))
        self.assertEqual(projection["latestOperation"]["action"], "workflow")

    def test_bounded_revoke_leaves_other_known_references_for_the_next_pass(self):
        for _ in range(21): self.issue()
        first = broker.revoke_generation_tokens(self.connection.pk, f"operation:{self.operation.pk}")
        self.assertEqual(first, {"revoked": 20, "pending": 1})
        second = broker.revoke_generation_tokens(self.connection.pk, f"operation:{self.operation.pk}")
        self.assertEqual(second, {"revoked": 1, "pending": 0})
        self.assertEqual(self.delete.call_count, 21)

    def test_changed_channel_binding_refuses_foreign_token_revocation(self):
        self.issue()
        self.channel().payload["workflow_operation_id"] = str(uuid4())
        with self.assertRaises(WebsiteAuthorityError):
            broker.revoke_generation_tokens(self.connection.pk, f"operation:{self.operation.pk}")
        self.delete.assert_not_called()

    def test_original_generation_credentials_can_be_retired_after_reconnect(self):
        self.issue()
        self.connection.generation += 1
        result = broker.revoke_generation_tokens(self.connection.pk, 3)
        self.assertEqual(result, {"revoked": 1, "pending": 0})
        self.assertEqual(self.tokens()[0].receipt["revocation_state"], "confirmed")

    def test_admitted_attempt_rebind_keeps_original_nonzero_inventory(self):
        self.issue()
        original = deepcopy(self.operation.payload[inventory.MARKER])
        self.operation.payload["attempt"] = 2
        self.data["operation_attempt"] = 2
        self.issue()
        self.assertEqual(self.operation.payload[inventory.MARKER], original)
        self.assertEqual(self.channel().receipt["token_count"], 2)
        self.assertEqual([r.payload["operation_attempt"] for r in self.tokens()], [1, 2])

    def test_cache_failure_after_mint_retires_bearer_without_returning_it(self):
        self.cache_set.side_effect = RuntimeError("Synthetic cache failure")
        with self.assertRaisesRegex(RuntimeError, "Synthetic cache failure"):
            self.issue()
        self.delete.assert_called_once()
        self.assertEqual(self.tokens()[0].receipt["revocation_state"], "confirmed")
        self.assertEqual(self.tokens()[0].receipt["mint_state"], "issued")

    def test_lost_original_marker_cannot_downgrade_a_known_channel_to_legacy(self):
        self.issue()
        self.operation.payload.pop(inventory.MARKER)
        with self.assertRaises(WebsiteAuthorityError): self.issue()
        self.assertEqual(self.mint.call_count, 1)
        self.assertEqual(self.channel().receipt["token_count"], 1)
