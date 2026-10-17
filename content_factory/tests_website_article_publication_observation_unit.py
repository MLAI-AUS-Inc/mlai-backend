"""Real refresh/controller path with fresh provider reads and scoped DB seams."""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from django.test import SimpleTestCase, override_settings

from . import website_article_publication_observation as observation
from . import tests_website_article_refresh_unit as fixtures
from .article_source_refresh_contract import digest
from .website_contract import WebsiteAuthorityError, evidence_digest


@override_settings(ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=True, ARTICLES_SETUP_CONSENT_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True, ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True, ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED=True, ARTICLES_ARTICLE_RECOVERY_ENABLED=True,
    ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=True, ROO_API_KEY="fixture-service-key")
class ArticlePublicationObservationTests(SimpleTestCase):
    def setUp(self):
        real = observation.advance
        self.source = fixtures.ArticleRefreshCoordinatorTests("runTest")
        self.addCleanup(self.source.doCleanups)
        self.source.setUp()
        self.source.publication_observer.side_effect = real
        self.connection, self.parent = self.source.connection, self.source.parent
        self.connection.repository_id = int(self.connection.repository_id)
        self.source.run.run_request["repository_id"] = self.connection.repository_id
        self.rows, self.publications, self.references = [], [], []
        self.pulls, self.branch_heads = {}, {}
        stack = self.source.chain.fixture.stack
        self.ledger = stack.enter_context(patch.object(observation, "_mutation_rows", side_effect=lambda *args: self.rows))
        self.provider = stack.enter_context(patch.object(observation, "read_limited", side_effect=self.github))
        self.token = stack.enter_context(patch("integrations.services.github_app.create_installation_access_token",
            return_value=SimpleNamespace(token="synthetic-read-token")))
        self.revoke = stack.enter_context(patch("integrations.http_client.delete"))
        self.source.post.side_effect = self.worker

    def worker(self, *args, **kwargs):
        response = self.source.worker(*args, **kwargs)
        value = response.json()
        value.update(publications=deepcopy(self.publications), publication_refs=deepcopy(self.references))
        value["digest"] = digest({key: item for key, item in value.items() if key != "digest"})
        return httpx.Response(200, json=value)

    def github(self, method, url, **kwargs):
        self.assertEqual(self.source.depth, 0, "Provider HTTP must be outside authority transactions")
        self.assertEqual(method, "GET")
        self.assertLessEqual(kwargs["deadline"], 12)
        self.assertEqual(kwargs["maximum"], 65536)
        prefix = "https://api.github.com/repos/" + self.connection.github_repo
        repository = {"id": self.connection.repository_id, "full_name": self.connection.github_repo}
        if url == prefix:
            value = repository
        elif url.startswith(prefix + "/branches/"):
            from urllib.parse import unquote
            branch = unquote(url[len(prefix + "/branches/"):])
            value = {"name": branch, "commit": {"sha": self.branch_heads.get(branch, self.connection.verified_sha)}}
        else:
            number = int(url.rsplit("/", 1)[1])
            value = deepcopy(self.pulls[number])
        return httpx.Response(200, json=value)

    def effect(self, *, key="a", number=7, backend=True, worker=True, merged=False):
        mutation_id, branch, head = key * 64, "cf/article-" + key, key * 40
        repo = self.connection.github_repo
        item = {"mutation_id": mutation_id, "run_id": self.source.run.run_id,
            "base_sha": self.source.run.run_request["expected_source_sha"], "head_sha": head,
            "branch": branch, "pr_url": f"https://github.com/{repo}/pull/{number}",
            "status": "applied", "files_sha256": digest([])}
        if backend:
            self.rows.append(SimpleNamespace(connection_id=self.connection.pk, generation=self.connection.generation,
                operation_id=f"{self.connection.pk}:{mutation_id}", run_id=item["run_id"], base_sha=item["base_sha"],
                head_sha=head, branch=branch, pr_url=item["pr_url"], status="applied", files=[], patch_digest=evidence_digest([])))
        if worker:
            self.publications.append(item)
        self.references.append({"run_id": item["run_id"], "number": number, "url": item["pr_url"]})
        identity = {"id": self.connection.repository_id, "full_name": repo}
        self.branch_heads[branch] = head
        self.pulls[number] = {"number": number, "html_url": item["pr_url"],
            "head": {"ref": branch, "sha": head, "repo": identity},
            "base": {"ref": self.connection.branch, "repo": identity},
            "state": "closed" if merged else "open", "merged": merged,
            "merge_commit_sha": "d" * 40 if merged else None, "auto_merge": {"enabled_by": {"login": "private-provider-canary"}}}
        return item

    def advance(self):
        return self.source.advance()

    def inspect_then_observe(self):
        self.advance()
        self.assertEqual(self.parent.receipt["article_source_refresh_stage"], "inspected")
        self.advance()
        return self.parent.receipt[observation.KEY]

    def test_actual_controller_unions_both_ledgers_and_reads_exact_old_pr_without_writes(self):
        self.effect()
        self.effect(key="b", number=8, backend=False)
        self.effect(key="c", number=9, worker=False, merged=True)
        value = self.inspect_then_observe()
        self.assertEqual(self.parent.receipt["article_source_refresh_stage"], "publication_observed")
        self.assertEqual(value["recorded_mutations"], 3)
        self.assertEqual(value["unpaired_mutations"], ["b" * 64, "c" * 64])
        self.assertEqual(value["unattributed_pulls"], [8])
        self.assertTrue(value["pulls"][2]["merged"])
        self.assertEqual(value["pulls"][2]["merge_sha"], "d" * 40)
        self.assertFalse(value["effect_authority"]); self.assertFalse(value["absence_proved"])
        self.assertNotIn("private-provider-canary", json.dumps(value))
        self.assertEqual(self.source.chain.fixture.spend.call_count, 1)
        self.token.assert_called_once_with(installation_id=self.connection.installation_id, repository=self.connection.github_repo,
            repository_id=self.connection.repository_id, permission_mode="read", use_cache=False)
        self.revoke.assert_called_once()
        calls = self.provider.call_count
        self.advance()
        self.assertEqual(self.provider.call_count, calls, "Fresh observations must not cause a token/poll storm")

    def test_missing_ledgers_keep_known_reference_unattributed_and_never_prove_absence(self):
        self.effect(backend=False, worker=False)
        value = self.inspect_then_observe()
        self.assertEqual(value["recorded_mutations"], 0)
        self.assertEqual(value["unattributed_pulls"], [7])
        self.assertFalse(value["absence_proved"])
        self.assertIn("cf/article-a", value["branches"])

    def test_empty_ledgers_are_unknown_even_after_repository_and_source_reads(self):
        value = self.inspect_then_observe()
        self.assertEqual(value["pulls"], [])
        self.assertFalse(value["absence_proved"])

    def test_fork_or_changed_head_cannot_become_owned_publication_evidence(self):
        self.effect()
        self.pulls[7]["head"]["repo"] = {"id": 999, "full_name": "foreign/site"}
        self.advance()
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.assertNotIn(observation.KEY, self.parent.receipt)
        self.revoke.assert_called_once()

    def test_new_contributor_head_is_visible_but_never_claimed_by_old_ledger(self):
        self.effect()
        self.pulls[7]["head"]["sha"] = "e" * 40
        value = self.inspect_then_observe()
        self.assertEqual(value["unattributed_pulls"], [7])
        self.assertFalse(value["pulls"][0]["branch_head_matches"])
        self.assertEqual(value["pulls"][0]["matching_backend_mutations"], [])

    def test_provider_404_or_private_error_body_cannot_prove_no_effect(self):
        self.advance()
        self.provider.side_effect = lambda *args, **kwargs: httpx.Response(404, text="private-provider-canary")
        with self.assertRaises(WebsiteAuthorityError) as error: self.advance()
        self.assertNotIn("private-provider-canary", str(error.exception))
        self.assertNotIn(observation.KEY, self.parent.receipt)
        self.revoke.assert_called_once()

    def test_provider_503_keeps_transient_failure_and_discards_private_body(self):
        self.advance()
        self.provider.side_effect = lambda *args, **kwargs: httpx.Response(503, text="private-provider-canary")
        with self.assertRaises(WebsiteAuthorityError) as error: self.advance()
        self.assertTrue(error.exception.retryable)
        self.assertNotIn("private-provider-canary", str(error.exception))
        self.assertNotIn(observation.KEY, self.parent.receipt)
        self.revoke.assert_called_once()

    def test_foreign_backend_mutation_is_refused_before_token_or_provider_read(self):
        self.effect()
        self.rows[0].connection_id = "foreign"
        self.advance()
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.token.assert_not_called(); self.provider.assert_not_called()

    def test_owner_cancel_during_provider_read_loses_commit_and_keeps_one_debit(self):
        self.advance()
        def cancelled(*args, **kwargs):
            response = self.github(*args, **kwargs)
            self.parent.state = "cancelled"
            return response
        self.provider.side_effect = cancelled
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.assertNotIn(observation.KEY, self.parent.receipt)
        self.assertEqual(self.source.chain.fixture.spend.call_count, 1)
        self.revoke.assert_called_once()

    def test_new_mutation_during_provider_read_loses_inventory_cas(self):
        self.advance()
        original = self.github
        def changed(*args, **kwargs):
            response = original(*args, **kwargs)
            if not self.rows: self.effect()
            return response
        self.provider.side_effect = changed
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.assertNotIn(observation.KEY, self.parent.receipt)

    def test_uncertain_provider_read_waits_then_reuses_original_inspection_operation(self):
        self.advance()
        self.provider.side_effect = observation.ServiceUnavailable("private-provider-canary")
        with self.assertRaises(WebsiteAuthorityError) as caught: self.advance()
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn("private-provider-canary", str(caught.exception))
        operation_ids = set(self.source.operations)
        self.provider.side_effect = self.github
        self.advance()
        self.assertEqual(self.token.call_count, 1)
        self.source.now += observation.LEASE + timedelta(seconds=1)
        self.advance()
        self.assertIn(observation.KEY, self.parent.receipt)
        self.assertEqual(set(self.source.operations), operation_ids)
        self.assertEqual(self.source.chain.fixture.spend.call_count, 1)

    def test_reused_inspection_rechecks_goal_budget_before_any_provider_token(self):
        self.advance()
        with patch("content_factory.website_goal_budget.exhaustion", return_value="deadline"):
            self.advance()
        self.token.assert_not_called(); self.provider.assert_not_called()

    def test_source_refresh_flag_alone_cannot_bypass_disabled_article_recovery(self):
        from . import website_article_refresh as refresh
        with override_settings(ARTICLES_ARTICLE_RECOVERY_ENABLED=False):
            self.assertFalse(refresh.advance(self.parent, self.source.chain.context, self.source.chain.config,
                user=self.source.chain.fixture.user, now=self.source.now))
        self.source.post.assert_not_called(); self.token.assert_not_called()
