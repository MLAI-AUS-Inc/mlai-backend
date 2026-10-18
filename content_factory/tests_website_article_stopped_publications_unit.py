"""Real paid stop/controller and mint journal with in-memory authority/ORM/provider seams."""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import website_article_stopped_publications as reads, website_article_source_stop as stops
from . import tests_website_article_source_stop_unit as fixtures
from . import tests_article_credential_inventory_unit as memory
from .website_contract import WebsiteAuthorityError, evidence_digest
from integrations.services.github_app import GitHubInstallationToken


@override_settings(**fixtures.ArticleSourceStopTests._overridden_settings)
class StoppedPublicationReadTests(SimpleTestCase):
    def setUp(self):
        f = fixtures.ArticleSourceStopTests("runTest")
        self.addCleanup(f.doCleanups)
        f.setUp()
        self.f = f
        self.parent, self.config, self.now = f.parent, f.config, f.now
        self.connection = self.parent.connection
        self.connection.repository_id = int(self.connection.repository_id)
        self.root = f.fixture.root
        self.stack = f.stack
        stops.advance(self.parent, self.config, now=self.now)
        self.parent.state = "running"  # the next prepare claim owns this pass
        self.rows, self.mutations, self.pulls = memory.Operations(), [], {}
        from .website_models import WebsiteConnectionOperation
        original_filter = WebsiteConnectionOperation.objects.filter
        def filtered(**fields):
            return self.rows.filter(**fields) if fields.get("action") == reads.ACTION else original_filter(**fields)
        self.stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "filter", side_effect=filtered))
        original_create = WebsiteConnectionOperation.objects.create
        self.stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "create", side_effect=lambda **kw:
            self.rows.create(**kw) if kw.get("action") == reads.ACTION else original_create(**kw)))
        original_locked = WebsiteConnectionOperation.objects.select_for_update()
        locked = SimpleNamespace(filter=lambda **kw: self.rows.filter(**kw) if kw.get("action") == reads.ACTION else original_locked.filter(**kw))
        locked.select_related = lambda *a: locked
        self.stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "select_for_update", return_value=locked))
        self.stack.enter_context(patch.object(reads, "_mutation_rows", side_effect=lambda *a: self.mutations))
        self.token = GitHubInstallationToken(token="synthetic-source-read-canary", expires_at=timezone.now() + timedelta(minutes=20),
            installation_id=self.connection.installation_id, repository=self.connection.github_repo)
        self.mint = self.stack.enter_context(patch("integrations.services.github_app.create_installation_access_token", side_effect=self.issue))
        self.provider = self.stack.enter_context(patch("content_factory.website_article_publication_observation.read_limited", side_effect=self.github))
        self.revoke = self.stack.enter_context(patch("integrations.http_client.delete", side_effect=self.retire))
        self.effect()

    def journal(self):
        return self.rows.rows[-1]

    def issue(self, **kw):
        self.assertEqual(self.f.fixture.fixture.depth, 0)
        row = self.journal()
        self.assertEqual(row.receipt["credential"]["mint_state"], "pending")
        self.assertEqual(row.payload["source_fence_id"], self.parent.receipt["article_source_fence_operation_id"])
        self.assertEqual(kw["permission_mode"], "read")
        self.assertFalse(kw["use_cache"])
        return self.token

    def retire(self, *a, **kw):
        self.assertEqual(self.f.fixture.fixture.depth, 0)
        self.assertEqual(kw["headers"]["Authorization"], "Bearer synthetic-source-read-canary")
        return Mock(status_code=204)

    def github(self, method, url, **kw):
        self.assertEqual(self.f.fixture.fixture.depth, 0)
        self.assertEqual(method, "GET")
        self.assertEqual(kw["headers"]["Authorization"], "Bearer synthetic-source-read-canary")
        self.assertEqual(kw["maximum"], 65536)
        self.assertLessEqual(kw["deadline"], 12)
        receipt = self.journal().receipt["credential"]
        self.assertTrue(receipt["delivery_possible"])
        self.assertEqual(receipt["expires_at"], self.token.expires_at.isoformat())
        repository = {"id": self.connection.repository_id, "full_name": self.connection.github_repo}
        value = deepcopy(self.pulls[int(url.rsplit("/", 1)[1])]) if "/pulls/" in url else repository
        return httpx.Response(200, json=value)

    def effect(self, number=42, key="d", *, backend=True):
        branch, head, repo = "cf/article-" + key, key * 40, self.connection.github_repo
        url = f"https://github.com/{repo}/pull/{number}"
        self.root.result["publish_pr_url"] = url
        if backend:
            self.mutations.append(SimpleNamespace(connection_id=self.connection.pk, generation=self.connection.generation,
                run_id=self.root.run_id, operation_id=f"{self.connection.pk}:{key * 64}", status="applied", files=[],
                patch_digest=evidence_digest([]), base_sha="a" * 40, head_sha=head, branch=branch, pr_url=url))
        identity = {"id": self.connection.repository_id, "full_name": repo}
        self.pulls[number] = {"number": number, "html_url": url, "head": {"ref": branch, "sha": head, "repo": identity},
            "base": {"ref": self.connection.branch, "repo": identity}, "merged": True, "state": "closed",
            "merge_commit_sha": "e" * 40, "auto_merge": {"enabled_by": {"login": "private-provider-canary"}}}

    def advance(self, seconds=0):
        return reads.advance(self.parent, self.config, now=self.now + timedelta(seconds=seconds))

    def test_stopped_source_observes_owned_merge_and_auto_merge_without_reviving_original(self):
        self.root.status = "cancelled"
        self.f.fixture.workflow.state = "cancelled"
        original = deepcopy(self.root.result)
        value = self.advance()
        pull = value["pulls"][0]
        self.assertTrue(pull["merged"]); self.assertTrue(pull["auto_merge_pending"])
        self.assertEqual(pull["matching_backend_mutations"], ["d" * 64])
        self.assertEqual(pull["merge_sha"], "e" * 40)
        self.assertEqual(self.root.status, "cancelled"); self.assertEqual(self.root.result, original)
        self.assertEqual(self.f.fixture.workflow.state, "cancelled")
        self.assertEqual(self.f.fixture.fixture.chain.fixture.spend.call_count, 1)
        for key in ("quiet", "inventory_complete", "effect_authority", "approval_carried"):
            self.assertIs(value[key], False)
        self.assertEqual(self.journal().receipt["credential"]["revocation_state"], "confirmed")
        self.assertNotIn("synthetic-source-read-canary", json.dumps(self.journal().receipt))
        self.assertNotIn("private-provider-canary", json.dumps(value))
        self.revoke.assert_called_once()

    def test_unpaired_reference_and_changed_head_remain_unattributed(self):
        self.pulls[42]["head"]["sha"] = "f" * 40
        self.effect(43, "b", backend=False)
        value = self.advance()
        self.assertEqual([row["matching_backend_mutations"] for row in value["pulls"]], [[], []])
        self.assertFalse(value["inventory_complete"])

    def test_empty_backend_history_never_mints_or_proves_absence(self):
        self.mutations.clear(); self.root.result.pop("publish_pr_url")
        value = self.advance()
        self.assertEqual(value["pulls"], []); self.assertFalse(value["quiet"])
        self.assertIsNone(self.journal().receipt["credential"])
        self.mint.assert_not_called(); self.provider.assert_not_called(); self.revoke.assert_not_called()

    def test_current_owner_stop_or_flag_rollback_refuses_new_purpose(self):
        with self.settings(ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=False):
            with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.parent.state = "cancelled"
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.mint.assert_not_called()

    def test_original_refund_prevents_mint(self):
        self.f.fixture.fixture.chain.fixture.refunds.return_value.exists.return_value = True
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.mint.assert_not_called()

    def test_refund_after_repository_read_stops_before_pr_read_but_retires_token(self):
        def refunded(*a, **kw):
            value = self.github(*a, **kw)
            self.f.fixture.fixture.chain.fixture.refunds.return_value.exists.return_value = True
            return value
        self.provider.side_effect = refunded
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.assertEqual(self.provider.call_count, 1)
        self.assertNotIn(reads.KEY, self.parent.receipt)
        self.assertEqual(self.journal().receipt["credential"]["revocation_state"], "confirmed")

    def test_stop_during_mint_prevents_get_but_settles_actual_issued_expiry(self):
        def stopped(**kw):
            value = self.issue(**kw)
            self.parent.state = "cancelled"
            return value
        self.mint.side_effect = stopped
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.provider.assert_not_called(); self.revoke.assert_called_once()
        self.assertEqual(self.journal().receipt["credential"]["expires_at"], self.token.expires_at.isoformat())

    def test_stop_after_pr_read_cannot_save_observation(self):
        def stopped(*a, **kw):
            value = self.github(*a, **kw)
            if "/pulls/" in a[1]: self.parent.state = "cancelled"
            return value
        self.provider.side_effect = stopped
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.assertNotIn(reads.KEY, self.parent.receipt); self.revoke.assert_called_once()

    def test_changed_inventory_during_mint_refuses_provider_read(self):
        def changed(**kw):
            value = self.issue(**kw)
            self.mutations[0].head_sha = "b" * 40
            return value
        self.mint.side_effect = changed
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.provider.assert_not_called(); self.revoke.assert_called_once()

    def test_changed_private_lease_refuses_commit(self):
        original = self.github
        def changed(*a, **kw):
            value = original(*a, **kw)
            stop = stops._operation(self.parent.receipt[stops.KEY])
            stop.receipt[reads.LEASE_KEY] = {**stop.receipt[reads.LEASE_KEY], "operation_id": "11111111-1111-4111-8111-111111111111"}
            return value
        self.provider.side_effect = changed
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.assertNotIn(reads.KEY, self.parent.receipt); self.revoke.assert_called_once()

    def test_lost_mint_reply_stays_unknown_and_lease_suppresses_duplicate_mint(self):
        from integrations import http_client
        self.mint.side_effect = http_client.RequestException("private-provider-canary")
        with self.assertRaises(WebsiteAuthorityError) as caught: self.advance()
        self.assertNotIn("private-provider-canary", str(caught.exception))
        self.assertEqual(self.journal().receipt["credential"]["mint_state"], "uncertain")
        self.assertIsNone(self.journal().receipt["credential"]["expires_at"])
        self.assertIsNone(self.advance(10)); self.assertEqual(self.mint.call_count, 1)
        self.mint.side_effect = self.issue
        self.advance(121)
        self.assertEqual(len(self.rows.rows), 2)
        self.assertEqual(self.rows.rows[0].receipt["credential"]["mint_state"], "uncertain")

    def test_missing_actual_expiry_is_never_used_and_still_revoked(self):
        self.mint.side_effect = None
        self.mint.return_value = SimpleNamespace(token="synthetic-source-read-canary")
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.provider.assert_not_called(); self.revoke.assert_called_once()
        self.assertEqual(self.journal().receipt["credential"]["mint_state"], "uncertain")

    def test_uncertain_revocation_keeps_actual_expiry_without_quiet(self):
        self.revoke.side_effect = None; self.revoke.return_value = Mock(status_code=404)
        value = self.advance()
        self.assertEqual(self.journal().receipt["credential"]["revocation_state"], "uncertain")
        self.assertFalse(value["quiet"])

    def test_missing_or_inaccessible_pr_is_not_absence_proof(self):
        self.provider.side_effect = lambda *a, **kw: httpx.Response(404, text="private-provider-canary")
        with self.assertRaises(WebsiteAuthorityError) as caught: self.advance()
        self.assertNotIn("private-provider-canary", str(caught.exception))
        self.assertNotIn(reads.KEY, self.parent.receipt); self.revoke.assert_called_once()

    def test_provider_throttle_remains_transient_without_an_owner_action(self):
        self.provider.side_effect = lambda *a, **kw: httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, text="private-provider-canary")
        with self.assertRaises(WebsiteAuthorityError) as caught: self.advance()
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn("private-provider-canary", str(caught.exception))
        self.assertNotIn("owner_action", self.parent.receipt); self.revoke.assert_called_once()

    def test_foreign_repository_response_never_becomes_original_evidence(self):
        self.pulls[42]["head"]["repo"] = {"id": 999, "full_name": "foreign/site"}
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.assertNotIn(reads.KEY, self.parent.receipt); self.revoke.assert_called_once()

    def test_fresh_saved_observation_rechecks_payment_without_repeating_http(self):
        first = self.advance()
        self.assertEqual(self.advance(60), first)
        self.assertEqual(self.mint.call_count, 1); self.assertEqual(self.provider.call_count, 2)
        self.f.fixture.fixture.chain.fixture.refunds.return_value.exists.return_value = True
        with self.assertRaises(WebsiteAuthorityError): self.advance(61)

    def test_rechecksummed_extra_provider_fields_cannot_enter_cached_metadata(self):
        from .website_goal import digest
        saved = self.advance()
        saved["pulls"][0]["private_provider_identity"] = "private-provider-canary"
        saved["digest"] = digest({k: v for k, v in saved.items() if k != "digest"})
        with self.assertRaises(WebsiteAuthorityError) as caught: self.advance(60)
        self.assertNotIn("private-provider-canary", str(caught.exception))
        self.assertEqual(self.mint.call_count, 1)

    def test_rechecksummed_quiet_or_approval_cannot_replace_an_observation(self):
        from .website_goal import digest
        saved = self.advance()
        saved["approval_carried"] = True
        saved["digest"] = digest({k: v for k, v in saved.items() if k != "digest"})
        with self.assertRaises(WebsiteAuthorityError): self.advance(60)
        self.assertEqual(self.mint.call_count, 1)

    def test_private_read_row_never_authorizes_generic_work_even_after_rollback(self):
        from .website_operations import validate_operation
        self.advance()
        row = self.journal()
        self.connection.operations.filter.side_effect = lambda **kw: memory.Rows([row])
        with self.settings(ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=False):
            for options in ({}, {"cancellation": True}, {"worker_cleanup": True}, {"restoration": True}):
                with self.assertRaises(WebsiteAuthorityError):
                    validate_operation(self.connection, {"operation_id": str(row.pk), "operation_attempt": 1}, **options)

    def test_mutation_with_foreign_run_or_original_base_is_refused_before_mint(self):
        self.mutations[0].base_sha = "f" * 40
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.mint.assert_not_called()

    def test_bounded_attempt_inventory_does_not_mint_unlimited_retries(self):
        self.advance()
        row = self.journal()
        self.rows.rows = [row] * reads.MAX_ATTEMPTS
        with self.assertRaises(WebsiteAuthorityError): self.advance(301)
        self.assertEqual(self.mint.call_count, 1)

    def test_changed_inventory_gets_a_distinct_fresh_attempt_after_lease_expiry(self):
        issue = self.issue
        def changed(**kw):
            value = issue(**kw)
            self.mutations[0].head_sha = "b" * 40
            return value
        self.mint.side_effect = changed
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.assertIsNone(self.advance(10))
        self.mint.side_effect = issue
        value = self.advance(121)
        self.assertEqual(len(self.rows.rows), 2)
        self.assertEqual(value["pulls"][0]["matching_backend_mutations"], [])

    def test_installation_change_during_mint_denies_get_and_retires_original_credential(self):
        def changed(**kw):
            value = self.issue(**kw)
            self.connection.installation_id = "999"
            return value
        self.mint.side_effect = changed
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.provider.assert_not_called(); self.revoke.assert_called_once()
        self.assertEqual(self.journal().receipt["credential"]["revocation_state"], "confirmed")

    def test_generation_change_cannot_redirect_final_credential_settlement(self):
        def changed(**kw):
            value = self.issue(**kw)
            self.connection.generation += 1
            return value
        self.mint.side_effect = changed
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.provider.assert_not_called(); self.revoke.assert_called_once()
        self.assertEqual(self.journal().receipt["credential"]["revocation_state"], "confirmed")
        self.assertNotEqual(self.journal().generation, self.connection.generation)

    def test_changed_current_source_cannot_commit_or_continue_provider_reads(self):
        def changed(*a, **kw):
            value = self.github(*a, **kw)
            self.connection.verified_sha = "f" * 40
            return value
        self.provider.side_effect = changed
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.assertEqual(self.provider.call_count, 1)
        self.assertNotIn(reads.KEY, self.parent.receipt); self.revoke.assert_called_once()

    def test_lost_prepare_claim_fails_real_claim_compare_before_saving_observation(self):
        from .website_models import WebsiteConnectionOperation
        original = WebsiteConnectionOperation.objects.select_for_update()
        lost = False
        def filtered(**kw):
            if lost and kw.get("action") == "prepare" and "updated_at" in kw:
                return memory.Rows([])
            return original.filter(**kw)
        query = SimpleNamespace(filter=filtered)
        query.select_related = lambda *a: query
        with patch.object(WebsiteConnectionOperation.objects, "select_for_update", return_value=query):
            def changed(*a, **kw):
                nonlocal lost
                value = self.github(*a, **kw)
                if "/pulls/" in a[1]: lost = True
                return value
            self.provider.side_effect = changed
            with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.assertNotIn(reads.KEY, self.parent.receipt); self.revoke.assert_called_once()

    def test_actual_stop_controller_seals_then_observes_through_new_private_purpose(self):
        from . import website_article_source_history as history
        from .tests_website_article_source_history_unit import worker_receipt
        from .website_prepare import _advance_claimed_prepare
        def operation(identifier, **kw):
            return next((row for row in self.f.fixture.reservations.values()
                if str(row.pk) == str(identifier) and row.action == history.ACTION), None)
        def sealed(method, url, *, body, **kw):
            self.assertTrue(url.endswith("/history-seal"))
            return SimpleNamespace(status_code=200, content=json.dumps(worker_receipt(body)).encode(), close=Mock())
        with patch.object(history, "_operation", side_effect=operation), patch.object(history, "read_limited", side_effect=sealed) as seal, \
                patch("content_factory.website_first_article_billing.reservation_ready", return_value=True), \
                patch("content_factory.website_prepare.journey_for_context", side_effect=AssertionError("No replacement journey")), \
                patch("content_factory.website_prepare._finish_prepare", return_value=self.parent):
            self.assertIs(_advance_claimed_prepare(self.parent, now=self.now, handlers=None), self.parent)
        self.assertIn("article_source_history", self.parent.receipt)
        self.assertIn(reads.KEY, self.parent.receipt)
        self.assertTrue(self.parent.receipt[reads.KEY]["pulls"][0]["merged"])
        seal.assert_called_once(); self.mint.assert_called_once()
        self.assertFalse(self.parent.receipt[reads.KEY]["effect_authority"])
