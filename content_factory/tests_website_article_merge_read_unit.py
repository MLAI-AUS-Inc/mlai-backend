"""Real paid guard -> original child -> broker -> owned PR observation -> retirement."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from roo.services import PointsService

from . import tests_article_credential_inventory_unit as fixtures
from . import website_first_article as article, website_first_article_billing as billing, website_tokens as tokens
from . import website_article_source_fence as fences
from .article_publish_approval import RECEIPT_KEY, make_article_publish_approval_receipt
from .website_operations import reserve_workflow_operation
from .website_connections import contract_for
from .website_contract import WebsiteAuthorityError


@override_settings(**fixtures.ArticleCredentialInventoryTests._overridden_settings)
class ArticleMergeReadTests(SimpleTestCase):
    def setUp(self):
        f = fixtures.ArticleCredentialInventoryTests("runTest")
        self.addCleanup(f.doCleanups)
        f.setUp()
        self.f = f
        self.now = timezone.now()
        self.owner = SimpleNamespace(pk=4, is_active=True)
        self.payer = SimpleNamespace(pk=5, is_active=True)
        self.context = SimpleNamespace(company=SimpleNamespace(pk=9), organization=f.organization)
        self.config = SimpleNamespace(website_connection=f.connection, website_connection_id=f.connection.pk)
        f.parent.payload["requested_by_user_id"] = "4"
        f.parent.attempts = 1
        f.parent.updated_at = f.parent._claimed_at = self.now
        f.parent._claimed_goal_revision = 0
        key = billing.request_key(f.parent.receipt["goal"])
        ledger = SimpleNamespace(pk=11, id=11, user_id=5, user=self.payer, kind="SPEND", source="CONTENT_FACTORY",
            reference_type="CONTENT_FACTORY", reference_id=key, idempotency_key="content_factory:charge:" + key,
            delta_microroo=-6_000_000)
        self.spend = f.start(patch.object(PointsService, "spend", return_value=(ledger, True)))
        f.start(patch("content_factory.billing.get_content_factory_article_cost_points", return_value=6))
        f.start(patch("content_factory.company_billing.billing_user_for_organization", return_value=self.payer))
        f.start(patch("founder_tools.services.founder_actor_id_for_user", return_value="founder:4"))
        f.start(patch("roo.models.Ledger.objects.select_for_update", return_value=fixtures.Rows([ledger])))
        self.refunded = False
        f.start(patch("roo.models.Ledger.objects.filter", side_effect=lambda **kw: fixtures.Rows([ledger] if self.refunded else [])))
        held = billing.reserve(f.parent, user=self.owner, now=self.now)
        request = {**contract_for(f.connection), "client_request_id": held["client_request_id"],
            "first_article_goal_id": str(f.parent.pk), "first_article_goal_operation_id": str(f.parent.pk),
            "expected_source_sha": "a" * 40, "delivery_mode": "publish_code"}
        self.workflow = reserve_workflow_operation(f.connection, workflow="direct_generate", payload=request)
        self.workflow.payload["run_id"] = "original-article"
        request.update(roo_points_ledger_id="11", roo_points_billing_user_id=5)
        self.source = SimpleNamespace(pk=101, run_id="original-article", organization_id=7, workflow="article_generation",
            domain="fixture.test", github_repo="fixture/site", status="completed", approval_state="approved", run_request=request,
            result={"generation": 1, "livePreview": {"previewUrl": "https://preview.test/article", "exactRender": True,
                "proof": {"commitSha": "b" * 40}, "resume_generation": 0},
                "article_preview_quality": {"status": "passed", "preview_url": "https://preview.test/article",
                    "inputs_sha256": "c" * 64, "resume_generation": 0}}, updated_at=self.now)
        self.source.run_request[RECEIPT_KEY] = make_article_publish_approval_receipt(self.source, actor_id="owner:4")
        f.parent.receipt.update(first_article_run_id=self.source.run_id, first_article_approval={
            "goal": article._immutable_binding(f.parent.receipt["goal"]), "review": article.article_review_identity(self.source),
            "source_sha": "a" * 40, "approval_digest": article.goals.digest(self.source.run_request[RECEIPT_KEY])})
        self.intent = {"pr_number": 42, "run_id": "publication-child", "website_connection_id": str(f.connection.pk),
            "connection_generation": 3, "repository_id": 42, "base_branch": "main", "source_sha": "a" * 40,
            "head_sha": "d" * 40, "head_branch": "cf/article"}
        self.child = SimpleNamespace(pk=102, run_id="publication-child", organization_id=7, workflow="article_generation",
            domain="fixture.test", github_repo="fixture/site", status="completed", run_request={**request, "source_run_id": self.source.run_id},
            result={"merge_status": "manual_merge_required", "publish_merge_intent": deepcopy(self.intent)}, save=Mock(), refresh_from_db=Mock())
        self.source.result["publish_child_run_id"] = self.child.run_id
        f.parent.receipt["first_article_publish_run_id"] = self.child.run_id
        records = [self.source, self.child]
        f.start(patch("workflow_runs.models.ContentFactoryRun.objects.filter", side_effect=lambda **kw: fixtures.Rows(records).filter(**kw)))
        f.start(patch("workflow_runs.models.ContentFactoryRun.objects.select_for_update", return_value=fixtures.Rows(records)))
        f.start(patch("content_factory.dispatch_models.ContentFactoryDispatchOutbox.objects.filter", return_value=fixtures.Rows([])))
        f.start(patch("content_factory.website_prepare._prepare_context", return_value=(self.context, self.config, self.owner)))
        repo = {"id": 42, "full_name": "fixture/site"}
        self.pull = {"number": 42, "merged": True, "merge_commit_sha": "e" * 40,
            "head": {"sha": "d" * 40, "ref": "cf/article", "repo": repo}, "base": {"ref": "main", "repo": repo}}
        self.response = Mock(status_code=200, headers={})
        self.response.json.side_effect = lambda: deepcopy(self.pull)
        self.get = f.start(patch("integrations.http_client.get", side_effect=self.read))

    def read(self, *args, **kwargs):
        self.assertEqual(self.f.depth, 0, "No site lock over owned PR HTTP")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer synthetic-token-canary")
        return self.response

    def observe(self):
        return article.observe_manual_merge(self.f.parent, self.child, now=self.now)

    def test_manual_merge_observer_accepts_only_exact_owned_pr_and_retires_its_broker_token(self):
        self.assertTrue(self.observe())
        self.assertEqual(self.child.result["merge_response"], {"merged": True, "sha": "e" * 40})
        self.f.mint.assert_called_once()
        self.assertEqual(self.f.mint.call_args.kwargs["permission_mode"], "read")
        self.assertFalse(self.f.mint.call_args.kwargs["use_cache"])
        self.get.assert_called_once(); self.f.delete.assert_called_once(); self.response.close.assert_called_once()
        self.assertEqual(self.f.tokens()[0].receipt["revocation_state"], "confirmed")
        self.assertEqual(self.f.tokens()[0].receipt["expires_at"], self.f.credential.expires_at.isoformat())
        self.assertEqual(self.spend.call_count, 1)

    def test_foreign_head_never_promotes_even_when_github_says_merged(self):
        self.pull["head"]["sha"] = "f" * 40
        self.assertFalse(self.observe())
        self.child.save.assert_not_called()
        self.assertEqual(self.child.result["merge_status"], "manual_merge_required")
        self.f.delete.assert_called_once()

    def test_source_fenced_original_never_mints_an_observation_credential(self):
        self.f.mint.side_effect = lambda **kw: self.f.credential
        self.f.parent.receipt[fences.KEY] = "11111111-1111-4111-8111-111111111111"
        with self.assertRaises(WebsiteAuthorityError): self.observe()
        self.f.mint.assert_not_called(); self.get.assert_not_called(); self.child.save.assert_not_called()

    def test_changed_stored_source_is_refused_without_an_untracked_head_probe(self):
        self.f.connection.verified_sha = "b" * 40
        with patch("content_factory.website_connections.verify_repository_head", side_effect=AssertionError("No validation token before original-purpose admission")) as probe:
            with self.assertRaises(WebsiteAuthorityError): self.observe()
        probe.assert_not_called(); self.f.mint.assert_not_called(); self.get.assert_not_called()

    def test_newer_branch_observation_overrides_the_older_verified_source(self):
        self.f.connection.scan_snapshots = fixtures.Rows([SimpleNamespace(generation=3, detector_version="github_head",
            source_sha="b" * 40, created_at=self.now)])
        with patch("content_factory.website_connections.verify_repository_head", side_effect=AssertionError("No secondary head token")) as probe:
            with self.assertRaises(WebsiteAuthorityError): self.observe()
        probe.assert_not_called(); self.f.mint.assert_not_called(); self.get.assert_not_called()

    def test_late_cancelled_child_cannot_be_reopened_by_owned_merge_response(self):
        self.f.mint.side_effect = lambda **kw: self.f.credential
        def cancelled(*args, **kwargs):
            result = self.read(*args, **kwargs)
            self.child.status = "cancelled"
            return result
        self.get.side_effect = cancelled
        with self.assertRaises(WebsiteAuthorityError): self.observe()
        self.assertEqual(self.child.status, "cancelled"); self.child.save.assert_not_called()
        self.assertEqual(self.f.tokens()[0].receipt["revocation_state"], "confirmed")

    def test_refund_during_provider_read_withholds_merge_and_retires_existing_token(self):
        self.f.mint.side_effect = lambda **kw: self.f.credential
        def refunded(*args, **kwargs):
            result = self.read(*args, **kwargs)
            self.refunded = True
            return result
        self.get.side_effect = refunded
        with self.assertRaises(WebsiteAuthorityError): self.observe()
        self.child.save.assert_not_called(); self.f.delete.assert_called_once()

    def test_changed_approval_during_read_refuses_promotion(self):
        def changed(*args, **kwargs):
            result = self.read(*args, **kwargs)
            self.source.run_request[RECEIPT_KEY] = {}
            return result
        self.get.side_effect = changed
        with self.assertRaises(WebsiteAuthorityError): self.observe()
        self.child.save.assert_not_called(); self.f.delete.assert_called_once()

    def test_changed_prepare_claim_during_read_refuses_promotion(self):
        def changed(*args, **kwargs):
            result = self.read(*args, **kwargs)
            self.f.parent.updated_at = self.now + timedelta(seconds=1)
            return result
        self.get.side_effect = changed
        with self.assertRaises(WebsiteAuthorityError): self.observe()
        self.child.save.assert_not_called(); self.f.delete.assert_called_once()

    def test_source_fence_during_mint_retires_without_reading_pr(self):
        def changed(**kwargs):
            result = self.f.provider(**kwargs)
            self.f.parent.receipt[fences.KEY] = "11111111-1111-4111-8111-111111111111"
            return result
        self.f.mint.side_effect = changed
        with self.assertRaises(WebsiteAuthorityError): self.observe()
        self.get.assert_not_called(); self.child.save.assert_not_called()
        self.assertEqual(self.f.tokens()[0].receipt["revocation_state"], "confirmed")

    def test_provider_timeout_is_retryable_and_never_exposes_provider_body(self):
        from integrations.http_client import exceptions
        self.get.side_effect = exceptions.Timeout("private-provider-canary")
        with self.assertRaises(WebsiteAuthorityError) as caught: self.observe()
        self.assertNotIn("private-provider-canary", str(caught.exception))
        self.assertTrue(caught.exception.retryable or getattr(caught.exception, "failure", {}).get("failure_class") == "transient")
        self.child.save.assert_not_called(); self.f.delete.assert_called_once()

    def test_controller_does_not_turn_provider_timeout_into_an_owner_merge_request(self):
        from integrations.http_client import exceptions
        from . import vibe_marketing_views as views
        self.get.side_effect = exceptions.Timeout("private-timeout-canary")
        with patch.object(views, "_publish_child_for_run_state", return_value=self.child), \
                patch.object(views, "_maybe_auto_merge_publish_child"), self.assertRaises(WebsiteAuthorityError):
            article._advance_git_first_article(self.f.parent, self.context, self.config, user=self.owner, now=self.now)
        self.assertEqual(self.f.parent.state, "running")
        self.assertNotIn("user_action", self.f.parent.receipt)
        self.child.save.assert_not_called()


@override_settings(**fixtures.ArticleCredentialInventoryTests._overridden_settings)
class BorrowedRepositoryReadTests(SimpleTestCase):
    def setUp(self):
        self.f = fixtures.ArticleCredentialInventoryTests("runTest")
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()

    def test_cache_eviction_cannot_hide_the_locally_borrowed_credential_from_retirement(self):
        with tokens.repository_read_credential(self.f.data) as issued:
            reference = issued["credential_reference"]
            self.f.cached.clear()
        self.f.delete.assert_called_once()
        self.assertEqual(str(self.f.tokens()[0].pk), reference)
        self.assertEqual(self.f.tokens()[0].receipt["revocation_state"], "confirmed")

    def test_stored_source_observation_mode_cannot_authorize_a_write(self):
        from .website_connections import authority_guard
        with self.assertRaises(WebsiteAuthorityError):
            with authority_guard(self.f.data, action="publish", stored_source_only=True): pass
        self.f.mint.assert_not_called()

    def test_each_nested_read_retires_only_its_own_credential(self):
        with tokens.repository_read_credential(self.f.data) as outer:
            self.f.credential = type(self.f.credential)(token="synthetic-second-read", expires_at=self.f.credential.expires_at,
                installation_id="4", repository="fixture/site")
            with tokens.repository_read_credential(self.f.data) as inner:
                self.assertNotEqual(outer["credential_reference"], inner["credential_reference"])
            self.assertEqual(self.f.tokens()[1].receipt["revocation_state"], "confirmed")
            self.assertEqual(self.f.tokens()[0].receipt["revocation_state"], "not_attempted")
            self.assertIn(f"website-issued-token:{outer['credential_reference']}", self.f.cached)
        self.assertEqual(self.f.delete.call_count, 2)
        self.assertEqual(self.f.tokens()[0].receipt["revocation_state"], "confirmed")

    def test_changed_yielded_mapping_cannot_substitute_a_foreign_token_or_reference(self):
        with tokens.repository_read_credential(self.f.data) as issued:
            issued.update(github_token="foreign-private-token-canary", credential_reference="foreign-reference")
        self.assertEqual(self.f.delete.call_args.kwargs["headers"]["Authorization"], "Bearer synthetic-token-canary")
        self.assertEqual(self.f.tokens()[0].receipt["revocation_state"], "confirmed")

    def test_owner_stop_does_not_prevent_settlement_of_the_already_issued_token(self):
        with tokens.repository_read_credential(self.f.data):
            self.f.operation.state = "cancelled"
        self.assertEqual(self.f.tokens()[0].receipt["revocation_state"], "confirmed")

    def test_unconfirmed_retirement_stays_durable_for_existing_cleanup(self):
        self.f.delete.side_effect = lambda *a, **kw: SimpleNamespace(status_code=404)
        with tokens.repository_read_credential(self.f.data): pass
        self.assertEqual(self.f.tokens()[0].receipt["revocation_state"], "uncertain")
        self.assertIn(f"website-issued-token:{self.f.tokens()[0].pk}", self.f.cached)
        self.f.delete.side_effect = self.f.retire
        result = tokens.revoke_generation_tokens(self.f.connection.pk, f"operation:{self.f.operation.pk}")
        self.assertEqual(result, {"revoked": 1, "pending": 0})

    def test_failed_metadata_lookup_still_retires_owned_bearer_without_forging_confirmation(self):
        from . import website_article_credentials as inventory
        with patch.object(inventory, "discover", side_effect=RuntimeError("private-database-canary")), tokens.repository_read_credential(self.f.data):
            pass
        self.f.delete.assert_called_once()
        self.assertEqual(self.f.tokens()[0].receipt["revocation_state"], "not_attempted")
        self.assertIsNotNone(self.f.tokens()[0].receipt["expires_at"])
