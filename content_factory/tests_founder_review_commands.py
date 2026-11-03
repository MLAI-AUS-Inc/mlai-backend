"""Prove exact canonical review HTTP acceptance without duplicate provider effects.

Only outbound CF HTTP is synthetic. Authentication, SQL, current authority,
command receipts, image charges and original execution clocks are real.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
from threading import Barrier, Event, Thread
from types import SimpleNamespace
from uuid import uuid4
import hashlib
from unittest.mock import patch

from django.db import connections
from django.core.cache import cache
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient

from core.models import User
from roo.models import Ledger
from roo.services import PointsService
from workflow_runs.models import ContentFactoryRun
from . import tests_run_current_review as paired
from .founder_journey_testing import ALL_P1_FLAGS_ON
from .models import OrganizationContentConfig
from .run_reconciliation import reconcile_authoritative_run
from .run_verified_delivery import project_verified_delivery
from .self_heal_testing import make_p2_org
from .tests_run_event_authority import FLAGS

OBSERVATIONS = []


def observe(name, **facts):
    row = {"case": name, **facts}
    OBSERVATIONS.append(row)
    target = os.environ.get("MUTATION_REPLAY_OBSERVATIONS")
    if target:
        Path(target).write_text(json.dumps(OBSERVATIONS, indent=2, default=str) + "\n")


def authenticated_client(user):
    """Exercise the real account-session authentication and My startup facade."""
    from community_chat.account_sessions import issue_account_session
    from community_chat.models import CommunityMemberProfile
    CommunityMemberProfile.objects.update_or_create(user=user, defaults={"status": "approved"})
    credentials = issue_account_session(user, SimpleNamespace(client_id="mlai-chat-ios",
        installation_id=uuid4(), origin="mlaichat://callback", platform="ios", device_name="Synthetic replay audit",
        public_key=hashlib.sha256(f"synthetic-r5-replay:{user.pk}".encode()).hexdigest()))
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {credentials.access_token}")
    return client


def canonical_state(run):
    return ContentFactoryRun.objects.values().get(pk=run.pk)


@override_settings(**FLAGS, **ALL_P1_FLAGS_ON, ARTICLES_FREE_FIRST_ARTICLE_ENABLED=True,
    CONTENT_FACTORY_URL="https://cf.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-paired-key",
    CONTENT_FACTORY_IMAGE_REGENERATION_COST_POINTS=2)
class FounderReviewCommandTests(TransactionTestCase):
    fixture_name = "r3_owned_portable_review_approval.json"
    reads = paired.CurrentReviewTests.reads

    def setUp(self):
        paired.CurrentReviewTests.setUp(self)
        cache.clear()
        self.client = authenticated_client(self.user)
        with self.reads():
            self.assertEqual(reconcile_authoritative_run(self.run.run_id, refresh=True)["outcome"], "history_received")
            project_verified_delivery(self.run.run_id)
        self.run.refresh_from_db()
        self.revision = self.fixture["article_review_response"]["revision"]
        self.text = next(x for x in self.fixture["article_review_response"]["fields"] if x["kind"] == "text")
        self.image = next(x for x in self.fixture["article_review_response"]["fields"] if x["kind"] == "image")
        self.foreign = make_p2_org(domain="foreign-review-replay.example").owner

    def url(self, suffix):
        return f"/api/v1/my-startup/vibe-marketing/runs/{self.run.run_id}/{suffix}/?company_id={self.company.pk}"

    def post(self, suffix, body, *, key=None, user=None):
        client = self.client
        if user is not None:
            client = authenticated_client(user)
        return client.post(self.url(suffix), body, format="json", HTTP_IDEMPOTENCY_KEY=key or body["operationId"])

    def assert_clock_and_request(self, before):
        after = canonical_state(self.run)
        for field in ("status", "approval_state", "run_request", "created_at", "updated_at",
                      "last_event_emitted_at", "reconciled_at"):
            self.assertEqual(after[field], before[field], field)

    def test_comment_same_key_body_replays_and_changed_body_or_foreign_principal_refuses(self):
        from .models import VibeMarketingComponentComment
        body = {"operationId": "comment-audit-operation", "componentId": self.text["id"],
            "componentType": "text", "body": "Keep this grounded in the original task."}
        before = canonical_state(self.run)
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.post") as outbound:
            first = self.post("comments", body)
            replay = self.post("comments", body)
            conflict = self.post("comments", {**body, "body": "Different feedback under the same operation."})
            foreign = self.post("comments", body, user=self.foreign)
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(first.data, replay.data)
        self.assertEqual(conflict.status_code, 409, conflict.data)
        self.assertIn(foreign.status_code, (403, 404))
        self.assertEqual(VibeMarketingComponentComment.objects.filter(run=self.run).count(), 1)
        outbound.assert_not_called()
        self.assert_clock_and_request(before)
        observe("comment", statuses=[x.status_code for x in (first, replay, conflict, foreign)],
            database_comments=1, provider_posts=0, replay_identical=True,
            result="Local SQL unique UUID5(run,actor,operationId) and full payload conflict fence are proved.")

    def test_comment_after_account_deactivation_fails_real_session_auth_before_replay(self):
        from .models import VibeMarketingComponentComment
        body = {"operationId": "comment-auth-revoke-operation", "componentId": self.text["id"],
            "componentType": "text", "body": "Retain the accepted comment only."}
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.post") as outbound:
            first = self.post("comments", body)
            self.assertEqual(first.status_code, 201, first.data)
            User.objects.filter(pk=self.user.pk).update(is_active=False)
            replay = self.post("comments", body)
        self.assertEqual(replay.status_code, 401, replay.data)
        self.assertEqual(VibeMarketingComponentComment.objects.filter(run=self.run).count(), 1)
        outbound.assert_not_called()
        observe("comment_revoked_account_session", statuses=[first.status_code, replay.status_code],
            actual_rows=1, provider_posts=0, real_session_authentication=True)

    def test_comment_simultaneous_same_operation_creates_one_actual_row(self):
        from .models import VibeMarketingComponentComment
        body = {"operationId": "comment-concurrent-operation", "componentId": self.text["id"],
            "componentType": "text", "body": "Two actual HTTP requests, one comment."}
        barrier = Barrier(2)
        results, errors = [], []
        def worker():
            try:
                user = User.objects.get(pk=self.user.pk)
                client = authenticated_client(user)
                barrier.wait(timeout=10)
                value = client.post(self.url("comments"), body, format="json", HTTP_IDEMPOTENCY_KEY="concurrent-key")
                results.append((value.status_code, value.data))
            except Exception as exc:
                errors.append(repr(exc))
            finally:
                connections.close_all()
        with self.reads():
            threads = [Thread(target=worker) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(x[0] for x in results), [200, 201], results)
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(VibeMarketingComponentComment.objects.filter(run=self.run).count(), 1)
        observe("comment_concurrency", statuses=sorted(x[0] for x in results), actual_rows=1,
            finished_thread_connections_closed=True)




    def test_direct_review_prior_success_reads_back_without_resubmitting_after_revision_conflict(self):
        body = {"action": "editText", "operationId": "revision-conflict-audit-operation",
            "expectedRevision": self.revision, "fieldId": self.text["id"], "value": "A retained edit"}
        submitted = []
        def external(method, url, **kwargs):
            if method == "GET":
                return paired.response(self.fixture["article_review_response"])
            submitted.append(deepcopy(kwargs["json"]))
            if len(submitted) == 1:
                return paired.response(self.fixture["article_review_response"])
            return paired.response({"code": "revision_changed", "revision": "provider-next-revision",
                "detail": "The provider's current revision changed."}, status=409)
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.request", side_effect=external):
            first = self.post("article-review", body)
            replay = self.post("article-review", body)
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(len(submitted), 1)
        observe("review_prior_success_then_provider_revision_conflict", statuses=[first.status_code, replay.status_code],
            provider_submissions=1, original_expected_revision_retained=True,
            limit="The external revision conflict is simulated. The backend replays durable acceptance without another external mutation.")

    def test_image_unknown_external_ack_holds_one_charge_without_resubmitting(self):
        import requests
        PointsService.credit_purchased_topup(user=self.user, delta=20,
            description="Synthetic image unknown-ACK funding", idempotency_key="image-unknown-ack-funding")
        body = {"action": "regenerateImage", "operationId": "image-unknown-ack-operation",
            "expectedRevision": self.revision, "fieldId": self.image["id"],
            "instruction": "Keep this same approved visual brief.", "expectedCostPoints": 2}
        submitted = []
        def external(method, url, **kwargs):
            if method == "GET": return paired.response(self.fixture["article_review_response"])
            submitted.append(deepcopy(kwargs["json"]))
            if len(submitted) == 1: raise requests.Timeout("Synthetic CF accepted-or-unknown response loss")
            return paired.response(self.fixture["article_review_response"])
        before = canonical_state(self.run)
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.request", side_effect=external):
            first = self.post("article-review", body)
            replay = self.post("article-review", body)
        self.assertEqual(first.status_code, 502, first.data)
        self.assertEqual(replay.status_code, 409, replay.data)
        self.assertEqual(len(submitted), 1)
        self.assertEqual(Ledger.objects.filter(kind="SPEND", reference_type="CONTENT_FACTORY_IMAGE").count(), 1)
        self.assertFalse(Ledger.objects.filter(kind="REFUND", reference_type="CONTENT_FACTORY_IMAGE").exists())
        self.assert_clock_and_request(before)
        observe("image_unknown_external_ack", statuses=[first.status_code, replay.status_code],
            provider_submissions=1, actual_image_spends=1, refunds=0, original_operation_retained=True,
            limit="A genuinely unknown external effect stays held without another mutation or invented refund.")

    def _review_action(self, action):
        field = self.image if action in {"regenerateImage", "chooseImage"} else self.text
        body = {"action": action, "operationId": f"review-{action}-audit-operation", "expectedRevision": self.revision}
        if action == "editText": body.update(fieldId=field["id"], value="A precise updated title")
        if action == "editTextBatch": body.update(edits=[{"fieldId": field["id"], "value": "A precise updated title"}])
        if action == "regenerateImage":
            body.update(fieldId=field["id"], instruction="Keep the retained visual brief.", expectedCostPoints=2)
            PointsService.credit_purchased_topup(user=self.user, delta=20,
                description="Synthetic isolated image replay funding", idempotency_key="image-replay-funding")
        if action == "chooseImage": body.update(fieldId=field["id"], candidateId="synthetic-candidate")
        if action == "discardImage": body.update(candidateId="synthetic-candidate")
        if action in {"undo", "allowAI"}: body.update(fieldId=field["id"])
        before = canonical_state(self.run)
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.request",
                return_value=paired.response(self.fixture["article_review_response"])) as remote:
            first = self.post("article-review", body)
            replay = self.post("article-review", body)
            changed_body = {**body, "instruction": "Changed instruction"} if action == "regenerateImage" else {**body, "expectedRevision": "different-revision"}
            changed = self.post("article-review", changed_body)
            foreign = self.post("article-review", body, user=self.foreign)
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertIn(foreign.status_code, (403, 404))
        posts = [call for call in remote.call_args_list if call.args[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(changed.status_code, 409, changed.data)
        if action == "regenerateImage":
            self.assertEqual(changed.status_code, 409, changed.data)
            self.assertEqual(Ledger.objects.filter(kind="SPEND", reference_type="CONTENT_FACTORY_IMAGE").count(), 1)
        self.assert_clock_and_request(before)
        wire_target = os.environ.get("REVIEW_ACTION_FIXTURE")
        if wire_target:
            target = Path(wire_target)
            records = json.loads(target.read_text()) if target.exists() else {}
            records[action] = {"source": "Actual bearer/facade/PostgreSQL and paired CF review fixture",
                "request": body, "idempotency_key": body["operationId"],
                "first": {"status": first.status_code, "body": first.data},
                "replay": {"status": replay.status_code, "body": replay.data},
                "changed_body_status": changed.status_code, "provider_posts": len(posts)}
            target.write_text(json.dumps(records, indent=2, default=str)+"\n")
        observe("review_" + action, statuses=[x.status_code for x in (first, replay, changed, foreign)],
            external_review_posts=len(posts), image_spends=Ledger.objects.filter(kind="SPEND", reference_type="CONTENT_FACTORY_IMAGE").count(),
            original_clocks_preserved=True,
            limit="One backend POST is proved; replay reads current authorized data and never needs CF mutation deduplication.")

    def edit_body(self, operation="stable-review-operation"):
        return {"action": "editText", "operationId": operation, "expectedRevision": self.revision,
            "fieldId": self.text["id"], "value": "A retained precise edit"}

    def test_header_must_match_the_original_body_operation(self):
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.request") as remote:
            refused = self.post("article-review", self.edit_body(), key="different-header-operation")
        self.assertEqual(refused.status_code, 409)
        self.assertFalse(any(call.args[0] == "POST" for call in remote.call_args_list))

    def test_current_account_revocation_prevents_accepted_replay(self):
        body = self.edit_body()
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.request",
                return_value=paired.response(self.fixture["article_review_response"])) as remote:
            self.assertEqual(self.post("article-review", body).status_code, 200)
            User.objects.filter(pk=self.user.pk).update(is_active=False)
            refused = self.post("article-review", body)
        self.assertEqual(refused.status_code, 401)
        self.assertEqual(sum(call.args[0] == "POST" for call in remote.call_args_list), 1)

    def test_unknown_effect_never_expires_into_another_post(self):
        from .founder_review_commands import KEY
        body = self.edit_body()
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.request",
                return_value=paired.response({"detail": "Unknown external acceptance"}, status=503)) as remote:
            first = self.post("article-review", body)
            self.assertEqual(first.status_code, 503)
            config = OrganizationContentConfig.objects.get(organization=self.org)
            strategy = deepcopy(config.pillar_strategy)
            record = next(iter(strategy[KEY].values()))
            self.assertEqual(record["state"], "unknown")
            record["lease_until"] = "2000-01-01T00:00:00+00:00"
            config.pillar_strategy = strategy
            config.save(update_fields=["pillar_strategy"])
            cache.clear()
            replay = self.post("article-review", body)
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(sum(call.args[0] == "POST" for call in remote.call_args_list), 1)

    def test_process_loss_after_external_success_does_not_duplicate_the_effect(self):
        body = self.edit_body()
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.request",
                return_value=paired.response(self.fixture["article_review_response"])) as remote:
            with patch("content_factory.founder_review_commands._finish", side_effect=RuntimeError("Synthetic process loss")):
                with self.assertRaises(RuntimeError):
                    self.post("article-review", body)
            cache.clear()
            replay = self.post("article-review", body)
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(sum(call.args[0] == "POST" for call in remote.call_args_list), 1)

    def test_malformed_saved_receipt_stays_untouched_without_effect(self):
        from .founder_review_commands import KEY
        body = self.edit_body()
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.request",
                return_value=paired.response(self.fixture["article_review_response"])) as remote:
            self.assertEqual(self.post("article-review", body).status_code, 200)
            config = OrganizationContentConfig.objects.get(organization=self.org)
            strategy = deepcopy(config.pillar_strategy)
            next(iter(strategy[KEY].values()))["state"] = "unrecognized"
            config.pillar_strategy = strategy
            config.save(update_fields=["pillar_strategy"])
            before = deepcopy(strategy)
            replay = self.post("article-review", body)
            config.refresh_from_db()
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(config.pillar_strategy, before)
        self.assertEqual(sum(call.args[0] == "POST" for call in remote.call_args_list), 1)

    def test_private_command_metadata_is_preserved_but_never_forwarded(self):
        from .editorial_catalog import merge_strategy, public_strategy
        from .founder_review_commands import KEY
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.request",
                return_value=paired.response(self.fixture["article_review_response"])):
            self.assertEqual(self.post("article-review", self.edit_body()).status_code, 200)
        strategy = OrganizationContentConfig.objects.get(organization=self.org).pillar_strategy
        self.assertNotIn(KEY, public_strategy(strategy))
        self.assertEqual(merge_strategy(strategy, {"generic": "new"})[KEY], strategy[KEY])

    def test_concurrent_same_command_has_one_external_post_and_no_cache_authority(self):
        body = self.edit_body("concurrent-review-operation")
        entered, release = Event(), Event()
        values, errors = [], []
        def external(method, url, **kwargs):
            if method == "POST":
                entered.set()
                if not release.wait(10):
                    raise AssertionError("Concurrent review request was not released")
            return paired.response(self.fixture["article_review_response"])
        def worker():
            try:
                client = authenticated_client(User.objects.get(pk=self.user.pk))
                value = client.post(self.url("article-review"), body, format="json",
                    HTTP_IDEMPOTENCY_KEY=body["operationId"])
                values.append(value.status_code)
            except Exception as exc:
                errors.append(repr(exc))
            finally:
                connections.close_all()
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.request", side_effect=external) as remote:
            thread = Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                cache.clear()
                pending = self.post("article-review", body)
                self.assertEqual(pending.status_code, 409)
            finally:
                release.set()
                thread.join(timeout=15)
            self.assertFalse(thread.is_alive())
            self.assertEqual(values, [200])
            self.assertEqual(errors, [])
            replay = self.post("article-review", body)
            self.assertEqual(replay.status_code, 200)
        self.assertEqual(sum(call.args[0] == "POST" for call in remote.call_args_list), 1)


    def comment_body(self, operation="comment-exact-revision-operation"):
        return {"operationId": operation, "componentId": self.text["id"],
            "componentType": "text", "componentLabel": "Article title",
            "expectedRevision": self.revision, "body": "Keep this grounded in the current review."}

    def test_full_client_comment_body_binds_revision_and_replays_original_row(self):
        from .models import VibeMarketingComponentComment
        body = self.comment_body()
        before = canonical_state(self.run)
        def read(method, url, **kwargs):
            self.assertEqual(method, "GET")
            self.assertFalse(connections["default"].in_atomic_block)
            return paired.response(self.fixture["article_review_response"])
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.request", side_effect=read) as remote:
            first = self.post("comments", body)
            replay = self.post("comments", body)
            changed = [self.post("comments", {**body, field: "Changed original intent"})
                for field in ("expectedRevision", "componentLabel", "body")]
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(first.data, replay.data)
        self.assertEqual([value.status_code for value in changed], [409, 409, 409])
        self.assertEqual(remote.call_count, 1)
        self.assertEqual(VibeMarketingComponentComment.objects.filter(run=self.run).count(), 1)
        self.assert_clock_and_request(before)
        target = os.environ.get("REVIEW_COMMENT_FIXTURE")
        if target:
            Path(target).write_text(json.dumps({"source": "Real bearer/facade/UTF8 PostgreSQL; paired CF read only",
                "request": body, "idempotency_key": body["operationId"],
                "first": {"status": first.status_code, "body": first.data},
                "replay": {"status": replay.status_code, "body": replay.data},
                "changed_variants": dict(zip(("expectedRevision", "componentLabel", "body"),
                    (value.status_code for value in changed))), "provider_posts": 0}, indent=2, default=str)+"\n")

    def test_first_comment_with_stale_revision_has_no_local_effect(self):
        from .models import VibeMarketingComponentComment
        body = {**self.comment_body(), "expectedRevision": "stale-original-revision"}
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.request",
                return_value=paired.response(self.fixture["article_review_response"])):
            response = self.post("comments", body)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertFalse(VibeMarketingComponentComment.objects.filter(run=self.run).exists())

    def test_replayed_comment_never_recreates_a_deleted_original_row(self):
        from .models import VibeMarketingComponentComment
        body = self.comment_body()
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.request",
                return_value=paired.response(self.fixture["article_review_response"])) as remote:
            first = self.post("comments", body)
            self.assertEqual(first.status_code, 201, first.data)
            VibeMarketingComponentComment.objects.filter(pk=first.data["id"]).delete()
            replay = self.post("comments", body)
        self.assertEqual(replay.status_code, 404, replay.data)
        self.assertFalse(VibeMarketingComponentComment.objects.filter(run=self.run).exists())
        self.assertEqual(remote.call_count, 1)

    def test_comment_row_and_acceptance_rollback_together_on_receipt_failure(self):
        from .models import VibeMarketingComponentComment
        body = self.comment_body()
        def broken_digest(value):
            if isinstance(value, dict) and "createdAt" in value:
                raise RuntimeError("Synthetic local receipt persistence failure")
            return actual_digest(value)
        from .founder_review_commands import _digest as actual_digest
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.request",
                return_value=paired.response(self.fixture["article_review_response"])), \
                patch("content_factory.founder_review_commands._digest", side_effect=broken_digest):
            with self.assertRaises(RuntimeError): self.post("comments", body)
        self.assertFalse(VibeMarketingComponentComment.objects.filter(run=self.run).exists())

    def test_comment_fresh_account_recheck_after_revision_read_prevents_local_write(self):
        from .models import VibeMarketingComponentComment
        def read(*args, **kwargs):
            User.objects.filter(pk=self.user.pk).update(is_active=False)
            return paired.response(self.fixture["article_review_response"])
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.request", side_effect=read):
            response = self.post("comments", self.comment_body())
        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(VibeMarketingComponentComment.objects.filter(run=self.run).exists())

    def test_comment_header_mismatch_refuses_before_revision_read(self):
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.request") as remote:
            response = self.post("comments", self.comment_body(), key="wrong-comment-operation")
        self.assertEqual(response.status_code, 409, response.data)
        remote.assert_not_called()

    def test_comment_simultaneous_exact_v2_command_creates_one_row(self):
        from .models import VibeMarketingComponentComment
        body = self.comment_body("concurrent-exact-comment-operation")
        barrier = Barrier(2)
        values, errors = [], []
        def read(*args, **kwargs):
            barrier.wait(10)
            return paired.response(self.fixture["article_review_response"])
        def worker():
            try:
                client = authenticated_client(User.objects.get(pk=self.user.pk))
                response = client.post(self.url("comments"), body, format="json", HTTP_IDEMPOTENCY_KEY=body["operationId"])
                values.append((response.status_code, response.data))
            except Exception as exc:
                errors.append(repr(exc))
            finally:
                connections.close_all()
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.request", side_effect=read):
            threads = [Thread(target=worker) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(value[0] for value in values), [200, 201], values)
        self.assertEqual(values[0][1], values[1][1])
        self.assertEqual(VibeMarketingComponentComment.objects.filter(run=self.run).count(), 1)


    def test_permission_revoked_immediately_before_image_debit_cannot_spend(self):
        from .article_review_billing import reserve_image_charge as original_reserve
        PointsService.credit_purchased_topup(user=self.user, delta=20,
            description="Synthetic current image-permission funding", idempotency_key="image-current-auth-funding")
        body = {"action": "regenerateImage", "operationId": "image-debit-revoke-operation",
            "expectedRevision": self.revision, "fieldId": self.image["id"],
            "instruction": "Use this exact retained brief.", "expectedCostPoints": 2}
        def revoke(**kwargs):
            User.objects.filter(pk=self.user.pk).update(is_active=False)
            return original_reserve(**kwargs)
        with self.reads(), patch("content_factory.article_review_billing.reserve_image_charge", side_effect=revoke), \
                patch("content_factory.vibe_marketing_views.http_client.request") as remote:
            response = self.post("article-review", body)
        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(Ledger.objects.filter(kind="SPEND", reference_type="CONTENT_FACTORY_IMAGE").exists())
        self.assertFalse(any(call.args[0] == "POST" for call in remote.call_args_list))

    def test_new_execution_ticket_cannot_reuse_old_review_acceptance(self):
        from founder_tools import startup_team
        from .run_execution_control import append_execution_ticket
        body = self.edit_body()
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.request",
                return_value=paired.response(self.fixture["article_review_response"])) as remote:
            first = self.post("article-review", body)
            self.assertEqual(first.status_code, 200, first.data)
            self.run.refresh_from_db()
            with startup_team._locked(self.org):
                append_execution_ticket(self.run, control_key="a" * 64,
                    reason="user_resume")
            replay = self.post("article-review", body)
        self.assertEqual(replay.status_code, 409, replay.data)
        self.assertEqual(sum(call.args[0] == "POST" for call in remote.call_args_list), 1)

    def test_enrolled_review_namespace_does_not_fall_through_after_flag_off(self):
        body = self.edit_body()
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.request",
                return_value=paired.response(self.fixture["article_review_response"])) as remote:
            self.assertEqual(self.post("article-review", body).status_code, 200)
            with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
                replay = self.post("article-review", body)
                unseen = self.post("article-review", self.edit_body("unseen-flagoff-review-operation"))
                comment = self.post("comments", self.comment_body())
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(unseen.status_code, 409, unseen.data)
        self.assertEqual(comment.status_code, 409, comment.data)
        self.assertEqual(sum(call.args[0] == "POST" for call in remote.call_args_list), 1)

    def test_corrupt_unhashable_receipt_state_refuses_without_repair_or_post(self):
        from .founder_review_commands import KEY
        body = self.edit_body()
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.request",
                return_value=paired.response(self.fixture["article_review_response"])) as remote:
            self.assertEqual(self.post("article-review", body).status_code, 200)
            config = OrganizationContentConfig.objects.get(organization=self.org)
            strategy = deepcopy(config.pillar_strategy)
            next(iter(strategy[KEY].values()))["state"] = ["invalid-state"]
            config.pillar_strategy = strategy
            config.save(update_fields=["pillar_strategy"])
            replay = self.post("article-review", body)
            config.refresh_from_db()
        self.assertEqual(replay.status_code, 409, replay.data)
        self.assertEqual(config.pillar_strategy, strategy)
        self.assertEqual(sum(call.args[0] == "POST" for call in remote.call_args_list), 1)


    def test_retained_comment_cannot_escape_by_omitting_revision_or_header(self):
        from .models import VibeMarketingComponentComment
        body = self.comment_body()
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.request",
                return_value=paired.response(self.fixture["article_review_response"])) as remote:
            first = self.post("comments", body)
            self.assertEqual(first.status_code, 201, first.data)
            for deleted in (False, True):
                if deleted: VibeMarketingComponentComment.objects.filter(pk=first.data["id"]).delete()
                for off_cohort in (False, True):
                    with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=not off_cohort):
                        missing_revision = {key: value for key, value in body.items() if key != "expectedRevision"}
                        refused = self.post("comments", missing_revision)
                        self.assertEqual(refused.status_code, 409, refused.data)
                        no_header = self.client.post(self.url("comments"), body, format="json")
                        self.assertEqual(no_header.status_code, 409, no_header.data)
                self.assertEqual(VibeMarketingComponentComment.objects.filter(run=self.run).count(), 0 if deleted else 1)
        self.assertEqual(remote.call_count, 1)

    def test_retained_review_cannot_change_action_or_remove_header_to_enter_another_handler(self):
        body = self.edit_body()
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.request",
                return_value=paired.response(self.fixture["article_review_response"])) as remote:
            self.assertEqual(self.post("article-review", body).status_code, 200)
            changed = self.post("article-review", {**body, "action": "applyUpdate"})
            no_header = self.client.post(self.url("article-review"), body, format="json")
        self.assertEqual(changed.status_code, 409, changed.data)
        self.assertEqual(no_header.status_code, 409, no_header.data)
        self.assertEqual(sum(call.args[0] == "POST" for call in remote.call_args_list), 1)


    def _connected_refresh(self, status, *, ready=False):
        from .website_models import WebsiteConnection, WebsiteConnectionOperation
        from .website_connections import contract_for
        from .run_ownership import bind_admitted_run, reserve_admitted_run, user_principal, current_execution_ticket
        from .run_event_authority import receive_authoritative_run_events
        from .tests_run_event_authority import batch
        website = WebsiteConnection.objects.create(organization=self.org, authorized_by=self.user,
            github_repo="synthetic/refresh-original", repository_id=42, installation_id="1001",
            branch="main", verified_sha="a" * 40)
        config = OrganizationContentConfig.objects.get(organization=self.org)
        config.website_connection, config.github_repo = website, website.github_repo
        config.save(update_fields=["website_connection", "github_repo"])
        operation = WebsiteConnectionOperation.objects.create(connection=website, generation=website.generation,
            action="workflow", state="failed", idempotency_key="connected-review-refresh-original",
            payload={"attempt": 1, "deletion_epoch": 0})
        raw = {**contract_for(website), "operation_id": str(operation.pk), "operation_attempt": 1,
            "deletion_epoch": 0, "domain": self.org.domain, "github_repo": website.github_repo,
            "client_request_id": "connected-review-refresh-original", "delivery_mode": "content_only",
            "expected_source_sha": "a" * 40, "topic": "Retain this connected review"}
        self.run = bind_admitted_run(reserve_admitted_run(organization=self.org,
            principal=user_principal(self.user.pk, f"mlai_user:{self.user.pk}"), workflow="article_generation",
            payload=raw), "connected-review-refresh-worker")
        operation.payload = {**operation.payload, "run_id": self.run.run_id}
        operation.save(update_fields=["payload"])
        self.assertEqual(receive_authoritative_run_events(batch(self.run, [{"status": status}])).status_code, 200)
        self.run.refresh_from_db()
        before = canonical_state(self.run)
        op_before = WebsiteConnectionOperation.objects.values().get(pk=operation.pk)
        ticket_before = current_execution_ticket(self.run)
        ledger_before = list(Ledger.objects.order_by("pk").values())
        body = {"action": "refresh", "operationId": "connected-preview-refresh-operation",
            "expectedRevision": self.revision}
        snapshot = {"revision": self.revision, "previewPending": not ready,
            "refreshError": None if ready else "Retry this preview"}
        with patch("content_factory.vibe_marketing_views.http_client.request",
                return_value=paired.response(snapshot)) as remote:
            first = self.post("article-review", body)
            replay = self.post("article-review", body)
        self.assertEqual(first.status_code, 200 if ready else 409, first.data)
        self.assertEqual(replay.status_code, 200 if ready else 409, replay.data)
        self.assertFalse(any(call.args[0] == "POST" for call in remote.call_args_list))
        self.assertEqual(current_execution_ticket(ContentFactoryRun.objects.get(pk=self.run.pk)), ticket_before)
        self.assertEqual(WebsiteConnectionOperation.objects.values().get(pk=operation.pk), op_before)
        self.assertEqual(list(Ledger.objects.order_by("pk").values()), ledger_before)
        self.assert_clock_and_request(before)

    def test_connected_failed_refresh_requires_explicit_resume_without_new_ticket_or_effect(self):
        self._connected_refresh("failed")

    def test_connected_blocked_refresh_requires_explicit_resume_without_new_ticket_or_effect(self):
        self._connected_refresh("blocked")

    def test_connected_ready_preview_refresh_is_readback_without_new_execution(self):
        self._connected_refresh("failed", ready=True)


for _action in ("editText", "editTextBatch", "regenerateImage", "chooseImage", "refresh", "undo", "discardImage", "allowAI"):
    def _case(self, action=_action): self._review_action(action)
    setattr(FounderReviewCommandTests, f"test_review_{_action}_actual_http", _case)
