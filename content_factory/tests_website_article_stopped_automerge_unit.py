"""Actual source-stop, paid scope, credential journal and simulated GitHub effects."""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
import httpx
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from . import website_article_stopped_automerge as merges, website_article_stopped_publications as reads
from . import website_article_source_stop as stops
from . import tests_website_article_stopped_publications_unit as read_tests
from .tests_article_credential_inventory_unit import Operations
from .website_contract import WebsiteAuthorityError
from .bounded_service_read import ServiceUnavailable
from integrations.services.github_app import GitHubInstallationToken


@override_settings(**read_tests.StoppedPublicationReadTests._overridden_settings)
class StoppedAutoMergeTests(SimpleTestCase):
    def setUp(self):
        self.reads = read_tests.StoppedPublicationReadTests("runTest")
        self.addCleanup(self.reads.doCleanups); self.reads.setUp()
        f = self.reads
        self.parent, self.config, self.now = f.parent, f.config, f.now
        self.connection, self.stack = f.connection, f.stack
        self.rows = Operations()
        from .website_models import WebsiteConnectionOperation
        original_filter, original_create = WebsiteConnectionOperation.objects.filter, WebsiteConnectionOperation.objects.create
        self.stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "filter", side_effect=lambda **kw:
            self.rows.filter(**kw) if kw.get("action") == merges.ACTION else original_filter(**kw)))
        self.stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "create", side_effect=lambda **kw:
            self.rows.create(**kw) if kw.get("action") == merges.ACTION else original_create(**kw)))
        original_locked = WebsiteConnectionOperation.objects.select_for_update()
        locked = SimpleNamespace(filter=lambda **kw: self.rows.filter(**kw) if kw.get("action") == merges.ACTION else original_locked.filter(**kw))
        locked.select_related = lambda *a: locked
        self.stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "select_for_update", return_value=locked))
        self.token = GitHubInstallationToken(token="synthetic-merge-stop-canary", expires_at=timezone.now()+timedelta(minutes=20),
            installation_id=self.connection.installation_id, repository=self.connection.github_repo,
            permissions={"contents":"read", "pull_requests":"write"}, permission_profile="pull_request_control")
        f.mint.side_effect = self.issue
        f.provider.side_effect = self.provider
        f.revoke.side_effect = self.retire
        self.graphql = self.stack.enter_context(patch.object(merges, "read_limited", side_effect=self.disable))
        f.pulls[42].update(node_id="PR_fixture_42", state="open", merged=False, merge_commit_sha=None)
        self.observation = f.advance()
        self.initial_mints = f.mint.call_count

    def latest(self): return self.rows.rows[-1]

    def issue(self, **kw):
        if kw.get("permission_profile") != "pull_request_control": return self.reads.issue(**kw)
        self.assertEqual(self.reads.f.fixture.fixture.depth, 0)
        self.assertEqual(self.latest().receipt["credential"]["mint_state"], "pending")
        self.assertEqual(kw["repository_id"], self.connection.repository_id)
        self.assertEqual(kw["permission_mode"], "write"); self.assertIs(kw["use_cache"], False)
        kw["before_issue"]()
        return self.token

    def provider(self, method, url, **kw):
        if kw["headers"]["Authorization"] != "Bearer synthetic-merge-stop-canary": return self.reads.github(method,url,**kw)
        self.assertEqual(self.reads.f.fixture.fixture.depth,0)
        self.assertEqual(method,"GET")
        receipt=self.latest().receipt["credential"]
        self.assertEqual(receipt["mint_state"],"issued"); self.assertTrue(receipt["delivery_possible"])
        self.assertEqual(receipt["expires_at"],self.token.expires_at.isoformat())
        repo={"id":self.connection.repository_id,"full_name":self.connection.github_repo}
        return httpx.Response(200,json=deepcopy(self.reads.pulls[int(url.rsplit('/',1)[1])]) if '/pulls/' in url else repo)

    def retire(self,*a,**kw):
        if kw["headers"]["Authorization"] != "Bearer synthetic-merge-stop-canary": return self.reads.retire(*a,**kw)
        self.assertEqual(self.reads.f.fixture.fixture.depth,0)
        return Mock(status_code=204)

    def disable(self,method,url,**kw):
        self.assertEqual(self.reads.f.fixture.fixture.depth,0)
        self.assertEqual((method,url),("POST","https://api.github.com/graphql"))
        row=self.latest(); self.assertEqual(row.receipt["mutation"]["state"],"possible")
        self.assertEqual(kw["body"]["variables"],{"id":"PR_fixture_42","nonce":str(row.pk)})
        self.assertIn("disablePullRequestAutoMerge",kw["body"]["query"])
        self.assertNotIn("enablePullRequestAutoMerge",kw["body"]["query"])
        self.reads.pulls[42]["auto_merge"]=None
        return httpx.Response(200,json={"data":{"disablePullRequestAutoMerge":{"clientMutationId":str(row.pk),"pullRequest":{"id":"PR_fixture_42"}}}})

    def advance(self,seconds=0):
        return merges.advance(self.parent,self.config,self.observation,now=self.now+timedelta(seconds=seconds))

    def test_original_owned_pending_auto_merge_is_withdrawn_without_reviving_old_workflow(self):
        self.reads.root.status="cancelled"; self.reads.f.fixture.workflow.state="cancelled"
        before=deepcopy(self.reads.root.result)
        value=self.advance()
        self.assertEqual(value["result"]["outcome"],"not_pending")
        self.assertEqual(self.latest().receipt["mutation"]["state"],"response_received")
        self.assertEqual(self.latest().receipt["credential"]["revocation_state"],"confirmed")
        self.assertEqual(self.latest().receipt["credential"]["mint_state"],"issued")
        self.assertEqual(self.reads.root.result,before); self.assertEqual(self.reads.root.status,"cancelled")
        self.assertEqual(self.reads.f.fixture.workflow.state,"cancelled")
        for field in ("quiet","inventory_complete","effect_authority","approval_carried"): self.assertIs(value[field],False)
        self.assertNotIn("synthetic-merge-stop-canary",json.dumps(self.latest().receipt))
        self.assertEqual(self.reads.f.fixture.fixture.chain.fixture.spend.call_count,1)
        self.graphql.assert_called_once()

    def test_mirrored_reference_without_matching_backend_mutation_does_not_mint_or_write(self):
        self.reads.mutations.clear(); self.observation=self.reads.advance(seconds=1)
        before=self.reads.mint.call_count
        self.assertIsNone(self.advance(1)); self.assertEqual(self.reads.mint.call_count,before)
        self.graphql.assert_not_called(); self.assertEqual(self.rows.rows,[])

    def test_head_moving_after_observation_is_recorded_without_mutation(self):
        self.reads.pulls[42]["head"]["sha"]="f"*40
        value=self.advance()
        self.assertEqual(value["result"]["outcome"],"changed"); self.assertTrue(value["result"]["auto_merge_pending"])
        self.graphql.assert_not_called()

    def test_pending_merge_disappearing_before_mutation_is_observed_without_post(self):
        self.reads.pulls[42]["auto_merge"]=None
        self.assertEqual(self.advance()["result"]["outcome"],"not_pending")
        self.graphql.assert_not_called()

    def test_merged_or_closed_pr_is_retained_and_not_modified(self):
        self.reads.pulls[42].update(state="closed",merged=True,merge_commit_sha="e"*40)
        self.assertEqual(self.advance()["result"]["outcome"],"merged")
        self.graphql.assert_not_called()

    def test_owner_refund_or_flag_withdrawal_prevents_new_credential(self):
        with self.settings(ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=False):
            with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.reads.f.fixture.fixture.chain.fixture.refunds.return_value.exists.return_value=True
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.assertEqual(self.reads.mint.call_count,self.initial_mints); self.graphql.assert_not_called()

    def test_revocation_after_pr_read_stops_before_post_and_retires_original_credential(self):
        def revoke(method,url,**kw):
            value=self.provider(method,url,**kw)
            if '/pulls/' in url: self.reads.f.fixture.fixture.chain.fixture.refunds.return_value.exists.return_value=True
            return value
        self.reads.provider.side_effect=revoke
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.graphql.assert_not_called()
        self.assertEqual(self.latest().receipt["credential"]["revocation_state"],"confirmed")
        self.assertNotIn(merges.KEY,self.parent.receipt)

    def test_lost_mutation_reply_is_reconciled_with_no_second_post_and_no_article_recharge(self):
        def lost(*a,**kw):
            self.disable(*a,**kw)
            raise ServiceUnavailable("synthetic lost reply")
        self.graphql.side_effect=lost
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        old=self.latest(); self.assertEqual(old.receipt["mutation"]["state"],"possible")
        self.assertEqual(old.receipt["credential"]["revocation_state"],"confirmed")
        self.assertIsNone(self.advance(1))
        self.assertEqual(self.advance(181)["result"]["outcome"],"not_pending")
        self.assertEqual(self.graphql.call_count,1)
        self.assertEqual(self.reads.f.fixture.fixture.chain.fixture.spend.call_count,1)

    def test_completed_subject_is_not_disabled_again(self):
        self.advance(); calls=self.reads.mint.call_count
        self.assertIsNone(self.advance(1)); self.assertEqual(self.reads.mint.call_count,calls)
        self.graphql.assert_called_once()

    def test_wrong_provider_repository_or_node_id_does_not_mutate(self):
        self.reads.pulls[42]["head"]["repo"]["id"]+=1
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.graphql.assert_not_called()
        self.assertEqual(self.latest().receipt["credential"]["revocation_state"],"confirmed")

    def test_graphql_errors_or_wrong_correlated_reply_stay_possible(self):
        self.graphql.side_effect=lambda *a,**kw:httpx.Response(200,json={"errors":[{"message":"private-provider-canary"}]})
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.assertEqual(self.latest().receipt["mutation"]["state"],"possible")
        self.assertNotIn("private-provider-canary",json.dumps(self.latest().receipt))
        self.assertNotIn(merges.KEY,self.parent.receipt)

    def test_expired_credential_cannot_be_used(self):
        self.token=GitHubInstallationToken(token="synthetic-merge-stop-canary",expires_at=timezone.now()-timedelta(seconds=1),
            installation_id=self.connection.installation_id,repository=self.connection.github_repo)
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.graphql.assert_not_called()
        self.assertEqual(self.latest().receipt["credential"]["mint_state"],"uncertain")
        self.assertEqual(self.latest().receipt["credential"]["revocation_state"],"confirmed")

    def test_completed_receipt_cannot_claim_quiet_or_carry_private_provider_fields(self):
        self.advance()
        row=self.latest(); value=deepcopy(row.receipt["result"]); value["quiet"]=True
        value["digest"]=reads.goals.digest({k:v for k,v in value.items() if k!="digest"})
        row.receipt["result"]=value
        with self.assertRaises(WebsiteAuthorityError): self.advance(1)
        self.graphql.assert_called_once()

    def test_private_operation_is_excluded_from_generic_schedulers(self):
        from .website_article_credentials import PRIVATE_ACTIONS
        self.assertIn(merges.ACTION,PRIVATE_ACTIONS)


    def test_malformed_provider_scalar_is_a_safe_denial_and_retires_token(self):
        self.reads.pulls[42]["html_url"]=["private-provider-canary"]
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.graphql.assert_not_called()
        self.assertEqual(self.latest().receipt["credential"]["revocation_state"],"confirmed")

    def test_unknown_mint_reply_never_reaches_provider_and_remains_recorded(self):
        def unknown(**kw):
            if kw.get("permission_profile")=="pull_request_control":
                from integrations import http_client
                raise http_client.RequestException("private-mint-canary")
            return self.reads.issue(**kw)
        self.reads.mint.side_effect=unknown
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.assertEqual(self.latest().receipt["credential"]["mint_state"],"uncertain")
        self.assertFalse(self.latest().receipt["credential"]["delivery_possible"])
        self.assertNotIn("private-mint-canary",json.dumps(self.latest().receipt))
        self.graphql.assert_not_called()

    def test_refund_during_mint_still_records_actual_expiry_and_retires_without_post(self):
        def withdrawn(**kw):
            value=self.issue(**kw)
            if kw.get("permission_profile")=="pull_request_control":
                self.reads.f.fixture.fixture.chain.fixture.refunds.return_value.exists.return_value=True
            return value
        self.reads.mint.side_effect=withdrawn
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        credential=self.latest().receipt["credential"]
        self.assertEqual(credential["expires_at"],self.token.expires_at.isoformat())
        self.assertFalse(credential["delivery_possible"])
        self.assertEqual(credential["revocation_state"],"confirmed")
        self.graphql.assert_not_called()

    def test_original_generation_is_used_for_retirement_after_new_generation_replaces_it(self):
        original_generation=self.connection.generation
        original=self.issue
        def changed(**kw):
            value=original(**kw)
            if kw.get("permission_profile")=="pull_request_control": self.connection.generation+=1
            return value
        self.reads.mint.side_effect=changed
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        row=self.latest()
        self.assertEqual(row.generation,original_generation)
        self.assertEqual(row.receipt["credential"]["revocation_state"],"confirmed")
        self.graphql.assert_not_called()

    def test_lost_prepare_claim_before_post_preserves_possible_delivery_without_sending(self):
        from . import website_first_article as first
        original=first._save_fields
        def save(parent,fields,**kwargs):
            if self.rows.rows and self.latest().receipt["mutation"]["state"]=="possible":
                raise WebsiteAuthorityError("website_operation_changed","Synthetic lost claim")
            return original(parent,fields,**kwargs)
        self.stack.enter_context(patch.object(first,"_save_fields",side_effect=save))
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.graphql.assert_not_called()
        self.assertEqual(self.latest().receipt["credential"]["revocation_state"],"confirmed")
        self.assertNotIn(merges.KEY,self.parent.receipt)

    def test_attempt_budget_prevents_new_mint_and_does_not_repeat_generation(self):
        self.stack.enter_context(patch.object(merges,"MAX_ATTEMPTS",1))
        self.graphql.side_effect=ServiceUnavailable("Synthetic transport unavailable")
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        calls=self.reads.mint.call_count
        with self.assertRaises(WebsiteAuthorityError): self.advance(181)
        self.assertEqual(self.reads.mint.call_count,calls)
        self.assertEqual(self.reads.f.fixture.fixture.chain.fixture.spend.call_count,1)

    def test_real_stop_controller_calls_read_then_distinct_merge_stop_after_seal(self):
        self.stack.enter_context(patch("content_factory.website_article_source_history.advance",return_value={"sealed":True}))
        self.stack.enter_context(patch.object(reads,"advance",return_value=self.observation))
        call=self.stack.enter_context(patch.object(merges,"advance",wraps=merges.advance))
        self.assertTrue(stops.advance(self.parent,self.config,now=self.now))
        self.assertEqual(call.call_count,1)
        self.assertEqual(self.parent.receipt[merges.KEY]["result"]["outcome"],"not_pending")
        self.graphql.assert_called_once()


    def test_expired_private_lease_stops_before_provider_mutation_and_retires_token(self):
        clock = {"now": timezone.now()}
        self.stack.enter_context(patch.object(merges,"timezone",SimpleNamespace(now=lambda:clock["now"],is_naive=timezone.is_naive)))
        def expired(method,url,**kw):
            value=self.provider(method,url,**kw)
            clock["now"]=self.now+merges.LEASE+timedelta(seconds=1)
            return value
        self.reads.provider.side_effect=expired
        with self.assertRaises(WebsiteAuthorityError): self.advance()
        self.graphql.assert_not_called()
        self.assertEqual(self.latest().receipt["credential"]["revocation_state"],"confirmed")

    def test_revoke_response_close_error_still_records_actual_retirement(self):
        def retired(*a,**kw):
            value=Mock(status_code=204)
            value.close.side_effect=RuntimeError("Synthetic response close failure")
            return value
        self.reads.revoke.side_effect=retired
        with self.assertRaises(RuntimeError): self.advance()
        self.assertEqual(self.latest().receipt["credential"]["revocation_state"],"confirmed")
        self.assertEqual(self.latest().receipt["credential"]["expires_at"],self.token.expires_at.isoformat())
        self.assertNotIn(merges.KEY,self.parent.receipt)

    def test_stopping_invalidates_latest_read_cache_but_retains_original_observation(self):
        original=deepcopy(self.observation)
        original_row=self.reads.journal()
        self.advance()
        stop=stops._operation(self.parent.receipt[stops.KEY])
        self.assertNotIn(reads.KEY,stop.receipt); self.assertNotIn(reads.KEY,self.parent.receipt)
        self.assertEqual(original_row.receipt["observation"],original)
        fresh=self.reads.advance(seconds=1)
        self.assertNotEqual(fresh["operation_id"],original["operation_id"])
        self.assertFalse(fresh["pulls"][0]["auto_merge_pending"])
        self.graphql.assert_called_once()
