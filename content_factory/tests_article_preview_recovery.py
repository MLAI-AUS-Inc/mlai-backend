"""A synthetic October draft keeps its eight sections and receives only a scoped, read-only preview."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch, Mock
from uuid import uuid4

from django.db import connection
from django.test import TestCase, override_settings, skipUnlessDBFeature
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from . import article_preview_recovery as recovery, article_preview_recovery_worker as worker
from .article_preview_recovery_state import effective
from .founder_journey_testing import make_new_journey_org
from .tests_founder_draft_tools import _run, _review
from .website_models import WebsiteConnection
from .website_operations import validate_operation
from .website_contract import WebsiteAuthorityError


@override_settings(
    ARTICLES_SAVED_PREVIEW_RECOVERY_ENABLED=True,
    ARTICLES_SAVED_PREVIEW_RECOVERY_DOMAINS="fixture-preview.example.com",
    ROO_API_KEY="fixture-service",
    CONTENT_FACTORY_URL="http://factory.test",
    CONTENT_FACTORY_API_KEY="fixture-worker",
)
class SavedPreviewRecoveryTests(TestCase):
    def setUp(self):
        self.startup = make_new_journey_org(domain="fixture-preview.example.com")
        self.org, self.cfg, self.user = self.startup.organization, self.startup.config, self.startup.owner
        self.site = WebsiteConnection.objects.create(
            organization=self.org,
            authorized_by=self.user,
            github_repo="fixture/editorial",
            repository_id=803,
            installation_id="14",
            branch="main",
            configuration_version=57,
            verified_sha="b" * 40,
            site_url="https://fixture.example",
            capabilities={"previewSupported": True, "publishingReady": True},
        )
        self.cfg.github_repo, self.cfg.website_connection, self.cfg.default_publish_target_id = (
            self.site.github_repo,
            self.site,
            "notes",
        )
        self.cfg.repo_execution_contract = {"runtime": "node", "build": ["npm", "run", "build"]}
        self.cfg.save()
        contract = {
            "target_id": "notes",
            "delivery_adapter": "react_component",
            "route_path": "/notes",
            "route_template": "/notes/{slug}",
            "registration_strategy": {"type": "registry_seo_patch", "registry_path": "src/notes.ts"},
        }
        self.target = self.site.targets.create(
            generation=1,
            target_key="notes",
            adapter="react_component",
            source_sha=self.site.verified_sha,
            verified_at=timezone.now(),
            capabilities={"previewSupported": True},
            contract=contract,
        )
        self.site.scan_snapshots.create(
            generation=1,
            run_id="old-scan",
            source_sha="a" * 40,
            fingerprint="c" * 64,
            evidence={
                "publish_targets": [contract],
                "repo_execution_contract": self.cfg.repo_execution_contract,
            },
        )
        self.run = _run(self.org, status="denied", workflow="direct_generate", delivery_mode="review_draft")
        self.run.github_repo = self.site.github_repo
        self.run.run_request.update(
            **recovery.contract_for(self.site),
            configuration_revision=34,
            github_installation_id="14",
            expected_source_sha="a" * 40,
            connection_target_id="notes",
            deletion_epoch=0,
            roo_points_requested_by_user_id=self.user.pk,
        )
        self.run.result = {
            "error_code": "website_source_changed",
            "article_preview_quality": {"status": "self_healing"},
            "live_preview": {"status": "building", "available": True, "platformStatus": "ready"},
        }
        self.run.save()
        self.review = _review(self.run.run_id, revision="3" * 64, media=[])
        self.review["previewRecoverySupported"] = True
        self.review["previewRecovery"] = {
            "runId": self.run.run_id,
            "revision": self.review["revision"],
            "state": "unavailable",
            "reason": "deployment_missing",
        }
        self.review.update(
            approved=False,
            previewPending=True,
            sections=[{"content": f"Saved section {i}"} for i in range(8)],
        )
        self.url = f"/api/v1/vibe-marketing/runs/{self.run.run_id}/article-review?company_id={self.startup.company.pk}"
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.remote = self.enterContext(
            patch("content_factory.article_review_views.remote_review", return_value=self.review)
        )
        self.native = self.enterContext(
            patch("content_factory.website_connections.verify_repository_native_target")
        )
        self.head = self.enterContext(
            patch(
                "content_factory.website_connections.verify_repository_head",
                return_value=self.site.verified_sha,
            )
        )
        self.submit = self.enterContext(patch("content_factory.website_wakeups.submit_operation"))

    def get(self):
        response = self.client.get(self.url + "&preview_recovery=1")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def effect(self, action, body, *, authorized=True):
        headers = {"HTTP_X_API_KEY": "fixture-service"} if authorized else {}
        return APIClient().post(
            f"/api/content-factory/runs/{self.run.run_id}/preview-recovery/{action}",
            body,
            format="json",
            **headers,
        )

    def test_historical_denial_reserves_one_read_only_lease_and_reads_back_exact_ready_receipt(self):
        original = deepcopy(self.run.run_request)
        with self.captureOnCommitCallbacks(execute=True):
            first = self.get()
            self.assertEqual(first["sections"], self.review["sections"])
            self.assertEqual(first["previewRecovery"]["state"], "building")
            again = self.client.post(
                self.url + "&preview_recovery=1",
                {"action": "recoverPreview", "expectedRevision": self.review["revision"]},
                format="json",
            )
            self.assertIn(again.status_code, (200, 202), again.content)
            self.assertEqual(again.json()["previewRecovery"], first["previewRecovery"])
        self.submit.assert_called_once()
        self.native.assert_not_called()
        op = recovery.current(self.run)
        wire = op.payload["wire"]
        self.assertEqual(
            (wire["configuration_version"], wire["source_sha"], wire["directory"]), (57, "b" * 40, "/notes")
        )
        self.assertIn(self.effect("authorize", wire, authorized=False).status_code, {401, 403})
        self.assertEqual(self.effect("authorize", wire).status_code, 200)
        self.assertEqual(self.effect("authorize", {**wire, "source_sha": "e" * 40}).status_code, 409)
        with self.assertRaises(WebsiteAuthorityError):
            validate_operation(self.site, {**recovery.contract_for(self.site), "operation_id": str(op.pk)})
        with patch.object(worker.http_client, "post", return_value=Mock(status_code=202)) as dispatch:
            self.assertEqual(worker.advance(op.pk), "pending")
            dispatch.assert_called_once()
            self.assertEqual(dispatch.call_args.kwargs["json"], wire)
            self.assertIsNone(worker.advance(op.pk))
        self.review["previewRecovery"] = {
            "state": "ready",
            "runId": self.run.run_id,
            "operationId": str(op.pk),
            "revision": wire["revision"],
            "sourceSha": wire["source_sha"],
            "generation": wire["generation"],
            "attempt": 1,
            "verifiedUntil": (timezone.now() + timedelta(minutes=10)).isoformat(),
            "previewUrl": "https://preview.fixture.example/notes/saved",
        }
        self.assertEqual(
            self.effect("result", {**wire, "state": "ready", "reason": "preview_verified"}).status_code, 200
        )
        self.assertEqual(worker.advance(op.pk), "completed")
        ready = self.get()["previewRecovery"]
        self.assertEqual(
            (ready["state"], ready["previewRevision"], ready["pollAfterMs"]),
            ("site_ready", wire["revision"], None),
        )
        self.assertIn(self.run.run_id + "/live-preview/proxy/", ready["previewUrl"])
        self.assertTrue(recovery.readable(self.run))
        self.assertEqual(recovery.run_view(self.run), ready)
        self.run.refresh_from_db()
        self.assertEqual((self.run.status, self.run.run_request), ("denied", original))
        self.assertEqual(self.site.operations.filter(action=recovery.ACTION).count(), 1)
        self.assertEqual(
            self.effect("result", {**wire, "state": "unavailable", "reason": "lease_expired"}).status_code,
            200,
        )
        self.assertEqual(self.get()["previewRecovery"], ready)
        lease_path = self.url.split("?")[0] + "/preview-lease?company_id=" + str(self.startup.company.pk)
        lease = self.client.post(lease_path, {}, format="json")
        self.assertEqual(lease.status_code, 200, lease.content)
        from urllib.parse import urlsplit

        protected_path = urlsplit(lease.json()["url"]).path
        remote = Mock(
            status_code=200,
            content=b"<article>Saved section 1</article>",
            headers={"Content-Type": "text/html"},
        )
        with patch(
            "content_factory.vibe_marketing_views.http_client.request", return_value=remote
        ) as request:
            protected = APIClient().get(protected_path)
            self.assertEqual(protected.status_code, 200, protected.content)
            self.assertIn("/api/runs/" + self.run.run_id + "/live-preview/proxy/", request.call_args.args[1])
        self.assertEqual(
            self.effect("result", {**wire, "state": "unavailable", "reason": "preview_expired"}).status_code,
            200,
        )
        self.assertIn(APIClient().get(protected_path).status_code, (404, 409))
        next_view = self.get()["previewRecovery"]
        self.assertEqual(next_view["state"], "building")
        self.assertNotEqual(next_view["job"]["id"], str(op.pk))
        self.assertEqual(self.get()["previewRecovery"]["job"], next_view["job"])
        self.assertEqual(self.site.operations.filter(action=recovery.ACTION).count(), 2)

    def test_unbound_legacy_snapshot_keeps_draft_and_stops_polling(self):
        self.site.scan_snapshots.all().delete()
        response = self.get()
        self.assertEqual(response["previewRecovery"]["state"], "destination_required")
        self.assertIsNone(response["previewRecovery"]["pollAfterMs"])
        self.assertFalse(response["previewPending"])
        self.assertEqual(response["sections"], self.review["sections"])
        self.assertFalse(self.site.operations.filter(action=recovery.ACTION).exists())
        self.assertNotIn("previewRecovery", self.client.get(self.url).json())
        self.head.assert_not_called()

    def test_permission_revocation_destination_change_cancellation_and_edit_fence(self):
        self.get()
        op = recovery.current(self.run)
        wire = op.payload["wire"]
        for field, value in (
            ("branch", "other"),
            ("installation_id", "99"),
            ("repository_id", 99),
            ("generation", 2),
        ):
            original = getattr(self.site, field)
            setattr(self.site, field, value)
            self.site.save()
            self.assertEqual(self.effect("authorize", wire).status_code, 409, field)
            setattr(self.site, field, original)
            self.site.save()
        self.target.contract = {**self.target.contract, "route_path": "/changed"}
        self.target.save()
        self.assertEqual(self.effect("authorize", wire).status_code, 409)
        self.target.contract = {**self.target.contract, "route_path": "/notes"}
        self.target.save()
        self.run.status = "cancelled"
        self.run.save()
        self.assertEqual(self.get()["previewRecovery"]["reason"], "article_cancelled")
        self.assertEqual(self.effect("authorize", wire).status_code, 409)
        self.run.status = "denied"
        self.run.save()
        recovery.observed_edit(self.run, wire["revision"])
        self.assertEqual(self.effect("authorize", wire).status_code, 409)
        self.assertEqual(self.get()["previewRecovery"]["reason"], "revision_changed")

    def test_current_lease_and_actual_run_revision_are_required_for_newer_work(self):
        raw = {
            "state": "building",
            "reason": "building",
            "revision": self.review["revision"],
            "runId": self.run.run_id,
            "sourceSha": "b" * 40,
            "operationId": str(uuid4()),
            "generation": 1,
            "attempt": 1,
            "leaseExpiresAt": (timezone.now() + timedelta(seconds=50)).isoformat(),
        }
        snapshot = {**self.review, "previewRecovery": raw}
        self.assertEqual(effective(self.run, snapshot)["state"], "draft_ready")
        self.run.status = "running"
        self.assertEqual(effective(self.run, snapshot)["state"], "building")
        raw["revision"] = "a different edit"
        self.assertIsNone(effective(self.run, snapshot)["job"])

    def test_expired_lease_settles_without_worker_or_further_provider_calls(self):
        self.get()
        op = recovery.current(self.run)
        future = timezone.now() + timedelta(hours=1)
        worker.advance(op.pk, now=future)
        op.refresh_from_db()
        self.assertEqual(op.receipt, {"status": "settled", "reason": "lease_expired"})
        self.assertIsNone(self.get()["previewRecovery"]["pollAfterMs"])
        self.native.assert_not_called()

    def test_explicit_missing_history_confirmation_binds_displayed_destination_and_revision(self):
        self.site.scan_snapshots.all().delete()
        view = self.get()["previewRecovery"]
        self.assertIn("confirm_destination", view["actions"])
        offer = view["destinationOffer"]
        self.assertEqual(
            (offer["repository"], offer["directory"], offer["branch"]),
            ("fixture/editorial", "/notes", "main"),
        )
        command = {
            "action": "confirmPreviewDestination",
            "expectedRevision": self.review["revision"],
            "offerId": offer["offerId"],
        }
        for changed in ({"offerId": "0" * 64}, {"expectedRevision": "other"}):
            self.assertEqual(
                self.client.post(
                    self.url + "&preview_recovery=1", {**command, **changed}, format="json"
                ).status_code,
                409,
            )
        self.assertFalse(self.site.operations.filter(action=recovery.ACTION).exists())
        self.site.configuration_version += 1
        self.site.save()
        self.assertEqual(
            self.client.post(self.url + "&preview_recovery=1", command, format="json").status_code, 409
        )
        command["offerId"] = self.get()["previewRecovery"]["destinationOffer"]["offerId"]
        for _ in range(2):
            response = self.client.post(self.url + "&preview_recovery=1", command, format="json")
            self.assertEqual(response.status_code, 202, response.content)
        op = recovery.current(self.run)
        self.assertEqual(op.payload["destination_offer"], command["offerId"])
        self.assertEqual(self.effect("authorize", op.payload["wire"]).status_code, 200)
        self.assertEqual(self.site.operations.filter(action=recovery.ACTION).count(), 1)
        self.site.repository_id = 444
        self.site.save()
        self.assertNotIn("destinationOffer", self.get()["previewRecovery"])

    def _confirm(self, offer_id):
        return self.client.post(
            self.url + "&preview_recovery=1",
            {"action": "confirmPreviewDestination", "expectedRevision": self.review["revision"], "offerId": offer_id},
            format="json",
        )

    def _finish_failed_build(self):
        op = recovery.current(self.run)
        wire = op.payload["wire"]
        self.review["previewRecovery"] = {
            "runId": self.run.run_id,
            "revision": wire["revision"],
            "sourceSha": wire["source_sha"],
            "operationId": str(op.pk),
            "generation": wire["generation"],
            "attempt": wire["attempt"],
            "state": "unavailable",
            "reason": "site_build_failed",
        }
        response = self.effect("result", {**wire, "state": "unavailable", "reason": "site_build_failed"})
        self.assertEqual(response.status_code, 200, response.content)
        op.refresh_from_db()
        self.assertEqual(op.receipt, {"status": "settled", "reason": "build_failed"})
        return op

    def test_failed_build_requires_fresh_owner_consent_and_preserves_the_failed_attempt(self):
        self.site.scan_snapshots.all().delete()
        initial_offer = self.get()["previewRecovery"]["destinationOffer"]["offerId"]
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self._confirm(initial_offer).status_code, 202)
        failed = self._finish_failed_build()
        view = self.get()["previewRecovery"]
        self.assertEqual(view["actions"], ["review_draft", "confirm_destination"])
        self.assertIsNone(view["pollAfterMs"])
        self.assertEqual(view["state"], "destination_required")
        retry_offer = view["destinationOffer"]["offerId"]
        self.assertNotEqual(retry_offer, initial_offer)
        self.assertEqual(self.get()["previewRecovery"], view)
        self.assertEqual(self._confirm(initial_offer).status_code, 202)
        self.assertEqual(self._confirm("0" * 64).status_code, 409)
        self.assertEqual(self.site.operations.filter(action=recovery.ACTION).count(), 1)
        self.submit.assert_called_once()
        with self.captureOnCommitCallbacks(execute=True):
            for _ in range(2):
                self.assertEqual(self._confirm(retry_offer).status_code, 202)
        self.assertEqual(self.site.operations.filter(action=recovery.ACTION).count(), 2)
        self.assertEqual(self.submit.call_count, 2)
        replacement = recovery.current(self.run)
        self.assertEqual(replacement.payload["wire"]["generation"], failed.payload["wire"]["generation"] + 1)
        self.assertEqual(self.effect("result", {**failed.payload["wire"], "state": "ready", "reason": "preview_verified"}).status_code, 200)
        failed.refresh_from_db()
        self.assertEqual(failed.receipt, {"status": "settled", "reason": "build_failed"})
        result = self.get()
        self.assertEqual(result["previewRecovery"]["job"]["id"], str(replacement.pk))
        self.assertEqual(result["sections"], self.review["sections"])
        self.assertFalse(result["approved"])
        self.assertEqual(self.effect("authorize", failed.payload["wire"]).status_code, 409)
        self.assertEqual(self.effect("authorize", replacement.payload["wire"]).status_code, 200)
        wire = replacement.payload["wire"]
        self.review["previewRecovery"].update(
            operationId=str(replacement.pk),
            generation=wire["generation"],
            state="ready",
            reason="preview_verified",
            previewUrl="https://preview.fixture.example/notes/saved",
            verifiedUntil=(timezone.now() + timedelta(minutes=5)).isoformat(),
        )
        self.assertEqual(self.effect("result", {**wire, "state": "ready", "reason": "preview_verified"}).status_code, 200)
        self.assertEqual(worker.advance(replacement.pk), "completed")
        review = self.get()
        self.assertEqual(review["previewRecovery"]["state"], "site_ready")
        self.assertEqual(review["previewRecovery"]["previewRevision"], self.review["revision"])
        self.assertEqual(review["sections"], self.review["sections"])
        self.assertFalse(review["approved"])

    def test_failed_build_offer_requires_matching_provider_and_current_authority_and_is_bounded(self):
        self.get()
        self._finish_failed_build()
        offer = self.get()["previewRecovery"]["destinationOffer"]["offerId"]
        raw = self.review["previewRecovery"]
        for field, value in (
            ("sourceSha", "d" * 40), ("runId", "other"), ("revision", "other"),
            ("operationId", str(uuid4())), ("generation", 9), ("attempt", True),
            ("reason", "provider_unavailable"), ("state", "building"),
        ):
            with self.subTest(field=field), patch.dict(raw, {field: value}):
                self.assertNotIn("destinationOffer", self.get()["previewRecovery"])
                self.assertEqual(self._confirm(offer).status_code, 409)
        self.site.configuration_version += 1
        self.site.save()
        self.assertNotIn("destinationOffer", self.get()["previewRecovery"])
        self.assertEqual(self._confirm(offer).status_code, 409)
        self.site.configuration_version -= 1
        self.site.save()
        self.assertEqual(self._confirm(offer).status_code, 202)
        self._finish_failed_build()
        next_offer = self.get()["previewRecovery"]["destinationOffer"]["offerId"]
        self.assertNotEqual(next_offer, offer)
        self.assertEqual(self._confirm(next_offer).status_code, 202)
        self._finish_failed_build()
        exhausted = self.get()["previewRecovery"]
        self.assertNotIn("destinationOffer", exhausted)
        self.assertEqual(exhausted["reason"], "recovery_exhausted")
        self.assertEqual(self._confirm(offer).status_code, 409)
        self.assertEqual(self.site.operations.filter(action=recovery.ACTION).count(), 3)

    def test_credential_uses_only_selected_repository_read_and_revokes_a_raced_mint(self):
        self.get()
        wire = recovery.current(self.run).payload["wire"]
        token = Mock(token="disposable-fake-read")
        with patch(
            "integrations.services.github_app.create_installation_access_token", return_value=token
        ) as mint:
            response = self.effect("token", wire)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response.json()["permission_mode"], "read")
            self.assertEqual(
                mint.call_args.kwargs,
                {
                    "installation_id": "14",
                    "repository_id": 803,
                    "repository": "fixture/editorial",
                    "permission_mode": "read",
                    "use_cache": False,
                },
            )

            def revoke_during_mint(**kwargs):
                self.site.state = "revoked"
                self.site.save()
                return token

            mint.side_effect = revoke_during_mint
            with patch.object(worker.http_client, "delete") as revoke:
                response = self.effect("token", wire)
                self.assertEqual(response.status_code, 409)
                self.assertNotIn("github_token", response.json())
                revoke.assert_called_once()

    def test_feature_off_preserves_private_review_and_refuses_new_work(self):
        with self.settings(ARTICLES_SAVED_PREVIEW_RECOVERY_ENABLED=False):
            view = self.get()["previewRecovery"]
            self.assertIsNone(view["pollAfterMs"])
            command = {"action": "recoverPreview", "expectedRevision": self.review["revision"]}
            response = self.client.post(self.url + "&preview_recovery=1", command, format="json")
            self.assertEqual(response.status_code, 409)
            self.assertFalse(self.site.operations.filter(action=recovery.ACTION).exists())

    def test_retained_draft_download_remains_private_when_the_selected_website_is_gone(self):
        from io import BytesIO
        from zipfile import ZipFile

        self.site.state = "disconnected"
        self.site.save()
        url = self.url.replace("/article-review?", "/article/download?")
        with patch("content_factory.founder_draft_views.tools_enabled", return_value=False):
            result = self.client.get(url)
            self.assertEqual(result.status_code, 200, getattr(result, "content", b"")[:100])
            with ZipFile(BytesIO(result.content)) as saved:
                self.assertEqual(saved.read("article.md").decode(), self.review["articleExport"]["markdown"])
            self.assertIn(APIClient().get(url).status_code, (401, 403))
            self.run.result = {**self.run.result, "draft_deleted_at": timezone.now().isoformat()}
            self.run.save()
            self.assertIn(self.client.get(url).status_code, (404, 409))
        self.assertFalse(self.site.operations.filter(action=recovery.ACTION).exists())

    def test_healthy_legacy_site_and_portable_preview_keep_the_existing_review(self):
        self.run.status = "completed"
        self.run.save()
        self.review.update(previewPending=False, previewCommit="b" * 40)
        self.review.pop("previewRecovery", None)
        self.assertNotIn("previewRecovery", self.get())
        self.run.github_repo = ""
        self.run.run_request = {"delivery_mode": "content_only", "delivery_mode_confirmed": True}
        self.run.save()
        self.review["previewRecovery"] = {
            "state": "unavailable",
            "reason": "preview_expired",
            "runId": self.run.run_id,
            "revision": self.review["revision"],
        }
        self.assertNotIn("previewRecovery", self.get())
        self.assertFalse(self.site.operations.filter(action=recovery.ACTION).exists())

    def test_ordinary_owned_build_offers_edit_draft_but_historical_denial_is_read_only(self):
        self.run.status = "running"
        self.run.save()
        self.review["previewRecovery"] = {
            "state": "building",
            "reason": "building",
            "runId": self.run.run_id,
            "revision": self.review["revision"],
            "operationId": "active-build",
            "sourceSha": "b" * 40,
            "generation": 1,
            "attempt": 1,
            "leaseExpiresAt": (timezone.now() + timedelta(minutes=2)).isoformat(),
        }
        current = self.get()["previewRecovery"]
        self.assertEqual(current["state"], "building")
        self.assertIn("edit_draft", current["actions"])
        self.run.status = "denied"
        self.run.save()
        self.assertNotIn("edit_draft", self.get()["previewRecovery"]["actions"])

    def test_no_explicit_confirmation_can_change_the_original_installation_or_principal(self):
        self.site.scan_snapshots.all().delete()
        before = self.get()["previewRecovery"]["destinationOffer"]["offerId"]
        self.site.authorized_by = None
        self.site.save()
        self.assertEqual(self.get()["previewRecovery"]["state"], "access_required")
        self.site.authorized_by = self.user
        self.site.installation_id = "new"
        self.site.save()
        self.assertNotIn("destinationOffer", self.get()["previewRecovery"])
        command = {
            "action": "confirmPreviewDestination",
            "expectedRevision": self.review["revision"],
            "offerId": before,
        }
        self.assertEqual(
            self.client.post(self.url + "&preview_recovery=1", command, format="json").status_code, 409
        )
        self.assertFalse(self.site.operations.filter(action=recovery.ACTION).exists())

    def test_old_worker_or_unconfigured_builder_cannot_reserve_a_recovery(self):
        for capability in (None, False):
            self.review.pop("previewRecoverySupported", None)
            if capability is not None:
                self.review["previewRecoverySupported"] = capability
            response = self.get()
            self.assertNotIn("previewRecoverySupported", response)
            self.assertNotIn("rebuild_preview", response["previewRecovery"]["actions"])
            command = {"action": "recoverPreview", "expectedRevision": self.review["revision"]}
            self.assertEqual(
                self.client.post(self.url + "&preview_recovery=1", command, format="json").status_code, 409
            )
        self.assertFalse(self.site.operations.filter(action=recovery.ACTION).exists())

    def test_transient_and_validation_probe_never_consume_a_rebuild(self):
        self.review["previewRecovery"].update(reason="provider_unavailable")
        self.assertIsNone(self.get()["previewRecovery"]["job"])
        self.assertFalse(self.site.operations.filter(action=recovery.ACTION).exists())
        self.review["previewRecovery"].update(
            state="validation_pending",
            reason="validating_saved_preview",
            attempt=1,
            generation=0,
            operationId="probe-" + "a" * 24,
            sourceSha="a" * 40,
            leaseExpiresAt=(timezone.now() + timedelta(seconds=100)).isoformat(),
        )
        observed = self.get()["previewRecovery"]
        self.assertEqual(observed["job"]["generation"], 0)
        self.assertEqual(observed["state"], "building")
        self.assertFalse(self.site.operations.filter(action=recovery.ACTION).exists())
        self.review["previewRecovery"].update(state="unavailable", reason="provider_placeholder")
        self.assertEqual(self.get()["previewRecovery"]["state"], "building")
        self.assertEqual(self.site.operations.filter(action=recovery.ACTION).count(), 1)

    def test_canary_admits_only_the_exact_domain_and_removal_fences_pending_authority(self):
        with self.settings(
            ARTICLES_SAVED_PREVIEW_RECOVERY_DOMAINS="mlai.au,other.fixture-preview.example.com"
        ):
            self.assertEqual(self.get()["previewRecovery"]["state"], "draft_ready")
            self.assertFalse(self.site.operations.filter(action=recovery.ACTION).exists())
        self.get()
        wire = recovery.current(self.run).payload["wire"]
        with self.settings(ARTICLES_SAVED_PREVIEW_RECOVERY_DOMAINS="mlai.au"):
            self.assertEqual(self.effect("authorize", wire).status_code, 409)

    @skipUnlessDBFeature("has_select_for_update_of")
    def test_postgres_authority_locks_only_the_historical_run_and_selected_operation(self):
        self.get()
        op = recovery.current(self.run)
        with CaptureQueriesContext(connection) as queries:
            response = self.effect("authorize", op.payload["wire"])
        self.assertEqual(response.status_code, 200, response.content)
        from workflow_runs.models import ContentFactoryRun

        run_table = connection.ops.quote_name(ContentFactoryRun._meta.db_table)
        operation_table = connection.ops.quote_name(op._meta.db_table)
        locks = [row["sql"] for row in queries.captured_queries if "FOR UPDATE" in row["sql"]]
        for table in (run_table, operation_table):
            applicable = [sql for sql in locks if f"FROM {table} " in sql]
            self.assertTrue(applicable, table)
            for sql in applicable:
                self.assertEqual(sql.split(" FOR UPDATE ", 1)[1], f"OF {table}")
