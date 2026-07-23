"""Dispatch idempotency across the mlai↔content-factory seam (Phase 4.1).

The failure class under test: a queue POST whose response is lost AFTER
content-factory enqueued the run. Before this work, mlai treated it as a
failure — refunding Roo points and inventing a local `vibe-marketing-*` run id
while the real run kept executing untracked (a "ghost run"). Now every dispatch
carries a client_request_id, the POST retries transiently with the SAME key,
an ambiguous outcome is resolved by key lookup before ANY refund, and an
unresolved dispatch materializes a provisional run keyed by the token that
binds to the real run via the first callback or poll.
"""
from types import SimpleNamespace
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from content_factory.dispatch_binding import bind_dispatch_token_run, run_is_dispatch_token_keyed
from content_factory.models import ContentFactoryJob, OrganizationContentConfig
from content_factory.service_views import _sync_generation_callback_to_run
from content_factory.vibe_marketing_views import (
    CONTENT_FACTORY_DISPATCH_ABSENT_GRACE_SECONDS,
    _article_system_publish_targets_provisional,
    _fail_unconfirmed_dispatch_run,
    _lookup_content_factory_dispatch_by_key,
    _mint_dispatch_client_request_id,
    _queue_content_factory_run,
    _resolve_dispatch_token_run,
    _run_pending_remote_dispatch,
)
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus

User = get_user_model()

REMOTE_SETTINGS = dict(
    CONTENT_FACTORY_URL="https://content-factory.test",
    CONTENT_FACTORY_API_KEY="secret-key",
    IS_LOCAL_ENV=False,
)


class _Response(SimpleNamespace):
    text = ""

    @property
    def content(self):
        return b"{}"

    def json(self):
        return self.payload


def _lookup_response(key_status, run_id="", run_status=""):
    payload = {"key_status": key_status}
    if run_id:
        payload.update({"run_id": run_id, "job_id": run_id})
    if run_status:
        payload["run_status"] = run_status
    return _Response(status_code=200, payload=payload)


def _absent_404():
    return _Response(status_code=404, payload={"detail": "No dispatch recorded for this client_request_id"})


@override_settings(**REMOTE_SETTINGS)
class QueueDispatchIdempotencyTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="dispatch@example.com", password="password")
        self.org = Organization.objects.create(domain="example.com", name="Example")
        self.context = SimpleNamespace(
            profile=SimpleNamespace(user=self.user),
            organization=self.org,
        )
        self.config = SimpleNamespace(github_repo="acme/site")

    def _queue(self, *, payload, endpoint="article", workflow="article_generation", billing=True):
        billing_refund_context = None
        if billing:
            billing_refund_context = {
                "charged_user": self.user,
                "article_request": {"client_request_id": payload.get("client_request_id"), "domain": "example.com"},
                "reason": "Vibe Marketing article queue did not start.",
            }
        return _queue_content_factory_run(
            endpoint=endpoint,
            workflow=workflow,
            context=self.context,
            config=self.config,
            payload=payload,
            billing_refund_context=billing_refund_context,
        )

    def test_mint_dispatch_key_fits_varchar_100(self):
        key = _mint_dispatch_client_request_id("article_system_setup")
        self.assertLessEqual(len(key), 100)
        self.assertTrue(key.startswith("vibe-dispatch:article_system_setup:"))

    def test_lost_response_recovered_by_key_lookup_one_run_zero_refund(self):
        """THE exit-criterion scenario: CF enqueues, the response is dropped,
        the key lookup resolves the run — exactly one run, zero refund."""
        key = "vibe-article:1:lostresponse"
        payload = {"client_request_id": key, "domain": "example.com", "target_keyword": "kw"}
        posts = []

        def fake_post(url, **kwargs):
            posts.append(kwargs.get("json"))
            raise requests.exceptions.ReadTimeout("read timed out")

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post), patch(
            "content_factory.vibe_marketing_views.http_client.get",
            return_value=_lookup_response("dispatched", run_id="cf-run-real-1", run_status="queued"),
        ), patch(
            "content_factory.vibe_marketing_views._refund_roo_points_for_article_start"
        ) as refund:
            run = self._queue(payload=payload)

        self.assertEqual(run.run_id, "cf-run-real-1")
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        refund.assert_not_called()
        # The retry (if any) reused the SAME key — never a fresh one.
        self.assertTrue(all(body.get("client_request_id") == key for body in posts))

    def test_transient_failure_retries_post_with_same_key(self):
        key = "vibe-article:1:transient"
        payload = {"client_request_id": key, "domain": "example.com", "target_keyword": "kw"}
        posts = []

        def fake_post(url, **kwargs):
            posts.append(kwargs.get("json"))
            if len(posts) == 1:
                raise requests.exceptions.ConnectionError("connection refused")
            # The retry may hit content-factory's idempotent replay path: a 200
            # with the ORIGINAL run and idempotent=true (not a fresh 202).
            return _Response(
                status_code=200,
                payload={"run_id": "cf-run-real-2", "status": "queued", "idempotent": True},
            )

        with patch("content_factory.vibe_marketing_views.http_client.post", side_effect=fake_post), patch(
            "content_factory.vibe_marketing_views.http_client.get", return_value=_absent_404()
        ), patch("content_factory.vibe_marketing_views._refund_roo_points_for_article_start") as refund:
            run = self._queue(payload=payload)

        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]["client_request_id"], key)
        self.assertEqual(posts[1]["client_request_id"], key)
        self.assertEqual(run.run_id, "cf-run-real-2")
        refund.assert_not_called()

    def test_ambiguous_outcome_withholds_refund_and_keys_run_by_token(self):
        """Timeout + unreachable lookup: no refund, no invented id — the local
        run is keyed by the dispatch token and marked for poll-time resolution."""
        key = "vibe-article:1:ambiguous"
        payload = {"client_request_id": key, "domain": "example.com", "target_keyword": "kw"}

        with patch(
            "content_factory.vibe_marketing_views.http_client.post",
            side_effect=requests.exceptions.ReadTimeout("read timed out"),
        ), patch(
            "content_factory.vibe_marketing_views.http_client.get",
            side_effect=requests.exceptions.ConnectionError("lookup unreachable"),
        ), patch(
            "content_factory.vibe_marketing_views._refund_roo_points_for_article_start"
        ) as refund:
            run = self._queue(payload=payload)

        refund.assert_not_called()
        self.assertEqual(run.run_id, key)
        self.assertTrue(run_is_dispatch_token_keyed(run))
        self.assertTrue(_run_pending_remote_dispatch(run))
        stash = run.run_request.get("pending_billing_refund")
        self.assertEqual(stash["charged_user_id"], self.user.pk)
        self.assertNotIn("vibe-marketing-", run.run_id)

    def test_definitive_rejection_refunds_immediately(self):
        key = "vibe-article:1:rejected"
        payload = {"client_request_id": key, "domain": "example.com", "target_keyword": "kw"}

        with patch(
            "content_factory.vibe_marketing_views.http_client.post",
            return_value=_Response(status_code=422, payload={"detail": "A topic or custom_title is required."}),
        ) as post, patch(
            "content_factory.vibe_marketing_views.http_client.get"
        ) as lookup, patch(
            "content_factory.vibe_marketing_views._refund_roo_points_for_article_start"
        ) as refund:
            run = self._queue(payload=payload)

        refund.assert_called_once()
        self.assertEqual(post.call_count, 1)  # 4xx is deterministic — no retry
        lookup.assert_not_called()
        self.assertFalse(_run_pending_remote_dispatch(run))
        self.assertEqual(run.status, ContentFactoryRunStatus.BLOCKED)

    def test_unkeyed_scan_dispatch_stays_single_attempt(self):
        payload = {"domain": "example.com", "github_repo": "acme/site"}

        with patch(
            "content_factory.vibe_marketing_views.http_client.post",
            side_effect=requests.exceptions.ReadTimeout("read timed out"),
        ) as post, patch("content_factory.vibe_marketing_views.http_client.get") as lookup:
            run = self._queue(payload=payload, endpoint="scan", workflow="repo_scan", billing=False)

        self.assertEqual(post.call_count, 1)
        lookup.assert_not_called()
        # The token still keys the provisional record (no invented id), but scan
        # is not key-resolvable on the content-factory side: no pending marker.
        self.assertTrue(run.run_id.startswith("vibe-dispatch:repo_scan:"))
        self.assertFalse(_run_pending_remote_dispatch(run))


@override_settings(**REMOTE_SETTINGS)
class DispatchTokenBindingTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="binding@example.com", password="password")
        self.token = "vibe-dispatch:article_system_setup:bindme"
        self.run = ContentFactoryRun.objects.create(
            run_id=self.token,
            workflow="article_system_setup",
            domain="example.com",
            status=ContentFactoryRunStatus.BLOCKED,
            run_request={
                "client_request_id": self.token,
                "dispatch_pending_resolution": True,
                "pending_billing_refund": {
                    "kind": "",
                    "charged_user_id": self.user.pk,
                    "actor_id": "actor-1",
                    "reason": "Vibe Marketing article queue did not start.",
                    "article_request": {"client_request_id": self.token, "domain": "example.com"},
                },
            },
            result={"error": "dispatch unconfirmed"},
        )

    def test_poll_resolution_binds_token_run_to_real_run(self):
        with patch(
            "content_factory.vibe_marketing_views.http_client.get",
            return_value=_lookup_response("dispatched", run_id="cf-real-setup-1", run_status="queued"),
        ):
            bound = _resolve_dispatch_token_run(self.run)

        self.assertIsNotNone(bound)
        self.assertEqual(bound.pk, self.run.pk)  # same row, re-keyed in place
        self.assertEqual(bound.run_id, "cf-real-setup-1")
        self.assertEqual(bound.status, ContentFactoryRunStatus.QUEUED)
        self.assertFalse(run_is_dispatch_token_keyed(bound))
        self.assertEqual(ContentFactoryRun.objects.count(), 1)

    def test_poll_resolution_confirmed_absent_within_grace_stays_pending(self):
        with patch(
            "content_factory.vibe_marketing_views.http_client.get", return_value=_absent_404()
        ), patch("content_factory.vibe_marketing_views._refund_roo_points_for_article_start") as refund:
            resolved = _resolve_dispatch_token_run(self.run)

        self.assertIsNone(resolved)
        refund.assert_not_called()
        self.run.refresh_from_db()
        self.assertTrue(_run_pending_remote_dispatch(self.run))

    def test_poll_resolution_confirmed_absent_after_grace_fails_and_refunds_once(self):
        from datetime import timedelta

        aged = timezone.now() - timedelta(seconds=CONTENT_FACTORY_DISPATCH_ABSENT_GRACE_SECONDS + 5)
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(created_at=aged)
        self.run.refresh_from_db()

        with patch(
            "content_factory.vibe_marketing_views.http_client.get", return_value=_absent_404()
        ), patch("content_factory.vibe_marketing_views._refund_roo_points_for_article_start") as refund:
            failed = _resolve_dispatch_token_run(self.run)
            # A second poll must not double-refund (result flag guards it,
            # independent of the ledger idempotency backstop).
            _fail_unconfirmed_dispatch_run(failed)

        self.assertEqual(failed.status, ContentFactoryRunStatus.FAILED)
        self.assertTrue(failed.result.get("dispatch_confirmed_absent"))
        self.assertTrue(failed.result.get("dispatch_refund_processed"))
        self.assertFalse(_run_pending_remote_dispatch(failed))
        self.assertEqual(refund.call_count, 1)
        self.assertEqual(refund.call_args.kwargs["charged_user"], self.user)

    def test_callback_with_client_request_id_binds_token_run(self):
        synced = _sync_generation_callback_to_run(
            data={
                "run_id": "cf-real-gen-1",
                "client_request_id": self.token,
                "workflow": "article_system_setup",
                "domain": "example.com",
            },
            run_status=ContentFactoryRunStatus.RUNNING,
            step_status="running",
        )

        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        self.assertEqual(synced.pk, self.run.pk)
        self.assertEqual(synced.run_id, "cf-real-gen-1")

    def test_callback_binding_merges_when_real_run_already_exists(self):
        real = ContentFactoryRun.objects.create(
            run_id="cf-real-existing-1",
            workflow="article_system_setup",
            domain="",
            status=ContentFactoryRunStatus.RUNNING,
            run_request={},
        )

        merged = bind_dispatch_token_run(client_request_id=self.token, remote_run_id="cf-real-existing-1")

        self.assertEqual(merged.pk, real.pk)
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        # The provisional record's dispatch payload (billing lineage) survives.
        self.assertEqual(merged.run_request.get("client_request_id"), self.token)
        self.assertEqual(merged.domain, "example.com")

    def test_binding_rebinds_token_keyed_billing_job(self):
        ContentFactoryJob.objects.create(
            job_id=self.token,
            domain="example.com",
            status="queued",
            slack_user_id="",
            client_request_id=self.token,
            billing_status="charged",
            billing_amount=6,
        )

        bound = bind_dispatch_token_run(client_request_id=self.token, remote_run_id="cf-real-job-1")

        self.assertEqual(bound.run_id, "cf-real-job-1")
        self.assertTrue(ContentFactoryJob.objects.filter(job_id="cf-real-job-1").exists())
        self.assertFalse(ContentFactoryJob.objects.filter(job_id=self.token).exists())

    def test_bind_requires_token_keyed_run(self):
        normal = ContentFactoryRun.objects.create(
            run_id="cf-normal-1",
            workflow="article_generation",
            domain="example.com",
            status=ContentFactoryRunStatus.RUNNING,
            run_request={"client_request_id": "some-other-key"},
        )
        self.assertIsNone(bind_dispatch_token_run(client_request_id="cf-normal-1", remote_run_id="cf-elsewhere"))
        normal.refresh_from_db()
        self.assertEqual(normal.run_id, "cf-normal-1")


class DispatchLookupClassificationTest(TestCase):
    """_lookup_content_factory_dispatch_by_key outcome mapping."""

    remote_config = {"base_url": "https://content-factory.test", "enabled": True, "api_key_configured": True, "is_local_env": False}

    def _lookup(self, response=None, side_effect=None):
        with patch(
            "content_factory.vibe_marketing_views.http_client.get",
            return_value=response,
            side_effect=side_effect,
        ):
            return _lookup_content_factory_dispatch_by_key(self.remote_config, "vibe-dispatch:x:key")

    def test_dispatched(self):
        outcome, payload = self._lookup(_lookup_response("dispatched", run_id="r1"))
        self.assertEqual(outcome, "dispatched")
        self.assertEqual(payload["run_id"], "r1")

    def test_recorded_failure_is_absent(self):
        outcome, _ = self._lookup(_lookup_response("failed"))
        self.assertEqual(outcome, "absent")

    def test_claimed_is_unknown(self):
        outcome, _ = self._lookup(_lookup_response("claimed", run_id="r1"))
        self.assertEqual(outcome, "unknown")

    def test_lookup_endpoint_404_is_absent(self):
        outcome, _ = self._lookup(_absent_404())
        self.assertEqual(outcome, "absent")

    def test_legacy_run_not_found_404_is_unknown(self):
        """A content-factory WITHOUT the lookup endpoint routes the path to
        /api/runs/{run_id} — its 404 must NOT be read as confirmed-absent."""
        outcome, _ = self._lookup(_Response(status_code=404, payload={"detail": "Run not found"}))
        self.assertEqual(outcome, "unknown")

    def test_transport_error_is_unknown(self):
        outcome, _ = self._lookup(side_effect=requests.exceptions.ConnectionError("down"))
        self.assertEqual(outcome, "unknown")


class ProvisionalPublishTargetTest(TestCase):
    def test_provisional_target_flag_passthrough(self):
        provisional_config = SimpleNamespace(
            publish_targets=[{"id": "t1", "kind": "hook", "provisional": True}]
        )
        verified_config = SimpleNamespace(publish_targets=[{"id": "t1", "kind": "hook"}])
        bundle_only_config = SimpleNamespace(
            publish_targets=[{"id": "t1", "publish_capability": "bundle_only", "provisional": True}]
        )
        self.assertTrue(_article_system_publish_targets_provisional(provisional_config))
        self.assertFalse(_article_system_publish_targets_provisional(verified_config))
        # A bundle-only fallback is not a live surface — no provisional signal.
        self.assertFalse(_article_system_publish_targets_provisional(bundle_only_config))
        self.assertFalse(_article_system_publish_targets_provisional(SimpleNamespace(publish_targets=[])))

    def test_gate_meta_carries_provisional_flag(self):
        from content_factory.vibe_marketing_views import _article_system_setup_gate

        org = Organization.objects.create(domain="prov.example.com", name="Prov")
        config = OrganizationContentConfig.objects.create(
            organization=org,
            github_repo="acme/site",
            publish_targets=[{"id": "t1", "kind": "hook", "provisional": True}],
        )
        meta = _article_system_setup_gate(config, [], {})
        self.assertTrue(meta["publishTargetsProvisional"])
