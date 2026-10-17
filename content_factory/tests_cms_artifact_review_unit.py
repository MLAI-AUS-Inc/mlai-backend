"""Private native CMS collection uses current original intent and durable leases."""

from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from workflow_runs.models import ContentFactoryRun
from . import cms_artifact_review as review
from .channel_artifact_contract import digest
from .cms_artifact_contract import validate_handoff, CmsArtifactError
from .cms_native_html import native_fragment, preview_document, profile_receipt
from . import tests_channel_artifacts_unit as artifact_fixtures
# Load view decorators before the fixture replaces transaction.atomic.
from . import service_views as _service_views
from .tests_cms_capture_unit import Rows
from .tests_cms_connection_contract_unit import TEST_KEY
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnectionOperation

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    return json.loads((ROOT / "contracts/cms-article-artifact-v1.example.json").read_text())


def seal(value):
    value["preview_sha256"] = hashlib.sha256(value["preview_html"].encode()).hexdigest()
    value["digest"] = digest({key: item for key, item in value.items() if key != "digest"})
    return value


@override_settings(ARTICLES_CHANNEL_DISCOVERY_ENABLED=True, ARTICLES_PUBLISHING_CHANNELS_ENABLED=True,
    ARTICLES_CMS_CHANNEL_ENABLED=True, ARTICLES_CHANNEL_ARTIFACTS_ENABLED=True,
    CONNECTOR_CREDENTIAL_KEYS=json.dumps({"test": TEST_KEY}), CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID="test")
class CmsArtifactReviewTests(SimpleTestCase):
    reserve = artifact_fixtures.ChannelArtifactTests.reserve

    def setUp(self):
        artifact_fixtures.ChannelArtifactTests.setUp(self)
        self.parent = self.reserve()
        self.run = ContentFactoryRun(pk=16, run_id="original-run", organization=self.org, domain=self.org.domain,
            workflow="article_generation", status="completed", run_request=deepcopy(self.payload), result={"generation": 2})
        self.run.refresh_from_db = Mock()
        self.source_rows = Rows([self.run])
        self.value = fixture()
        self.value.update(run_id=self.run.run_id, run_generation=2, envelope=self.payload["publishing_channel_artifact"])
        seal(self.value)
        self.extra = [patch.object(ContentFactoryRun.objects, "filter", side_effect=self.source_rows.filter),
            patch.object(ContentFactoryRun.objects, "select_for_update", return_value=self.source_rows),
            patch.object(WebsiteConnectionOperation.objects, "select_for_update", return_value=self.manager),
            patch.object(review, "fetch_handoff", return_value=deepcopy(self.value))]
        values = [item.start() for item in self.extra]
        self.fetch = values[-1]
        for item in self.extra: self.addCleanup(item.stop)
        self.now = timezone.now() + timedelta(seconds=1)

    def start(self, **kwargs):
        return review.start_review(self.run, **kwargs)

    def advance(self, operation, **kwargs):
        return review.advance_review(operation.pk, now=kwargs.get("now", self.now))

    def test_private_collection_is_idempotent_bound_to_original_scope_and_never_approval(self):
        operation = self.start(expected_revision=self.value["review_revision"])
        self.assertIs(self.start(expected_revision=self.value["review_revision"]), operation)
        self.assertNotIn("handoff", operation.payload)
        self.fetch.assert_not_called()
        result = self.advance(operation)
        self.assertEqual(result.state, "completed")
        self.assertEqual(result.payload["handoff"], self.value)
        self.assertEqual(result.receipt["artifactDigest"], self.value["digest"])
        for key in ("approval_recorded", "provider_modified", "publishing_verified"):
            self.assertIs(result.receipt[key], False)
        self.assertNotIn("handoff", result.receipt)
        self.assertNotIn("Fixture guide", json.dumps(result.receipt))
        self.assertNotIn("synthetic-application-password", json.dumps(result.payload))
        self.assertNotIn("lease_id", result.payload)
        self.assertEqual(self.connection.verified_sha, "")
        self.fetch.assert_called_once_with(self.run, expected_revision=self.value["review_revision"])

    def test_disabled_collection_does_no_database_or_remote_work(self):
        with override_settings(ARTICLES_CHANNEL_ARTIFACTS_ENABLED=False), patch.object(WebsiteConnectionOperation.objects, "filter") as rows:
            self.assertIsNone(review.advance_review(self.parent.pk))
            self.assertEqual(review.ensure_reviews(), 0)
        rows.assert_not_called(); self.fetch.assert_not_called()

    def test_legacy_unversioned_incomplete_foreign_or_invalid_review_source_cannot_start(self):
        for field, value in (("status", "running"), ("result", {}), ("result", {"generation": True}),
                ("organization_id", 99), ("github_repo", "customer/site")):
            before = getattr(self.run, field); setattr(self.run, field, value)
            with self.subTest(field=field), self.assertRaises(WebsiteAuthorityError): self.start()
            setattr(self.run, field, before)
        for revision in (True, "synthetic-private-input", "f" * 63):
            with self.assertRaises(WebsiteAuthorityError): self.start(expected_revision=revision)
        self.fetch.assert_not_called()

    def test_changed_connection_before_claim_cancels_once_without_a_stuck_pending_read(self):
        operation = self.start()
        self.connection.state = "disconnected"
        result = self.advance(operation)
        self.assertEqual(result.state, "cancelled")
        self.assertEqual(result.payload["source_run_id"], self.run.run_id)
        self.assertNotIn("handoff", result.payload)
        self.fetch.assert_not_called()

    def test_changed_source_generation_owner_or_policy_after_http_discards_the_handoff(self):
        for change in ("generation", "owner", "policy", "envelope"):
            operation = self.start(expected_revision=self.value["review_revision"] if change != "generation" else None)
            original_generation = self.run.result["generation"]
            original_user = self.user.is_active
            original_policy = self.config.auto_publish
            def response(*args, **kwargs):
                if change == "generation": self.run.result["generation"] += 1
                elif change == "owner": self.user.is_active = False
                elif change == "policy": self.config.auto_publish = not self.config.auto_publish
                else: return seal({**deepcopy(self.value), "envelope": {**self.value["envelope"], "owner_user_id": "99"}})
                return deepcopy(self.value)
            self.fetch.side_effect = response
            result = self.advance(operation)
            self.assertEqual(result.state, "cancelled", change)
            self.assertNotIn("handoff", result.payload)
            self.run.result["generation"] = original_generation
            self.user.is_active, self.config.auto_publish = original_user, original_policy
            self.saved.remove(operation)

    def test_late_http_cannot_overwrite_an_explicit_cancellation(self):
        operation = self.start()
        def cancel(*args, **kwargs):
            operation.state, operation.payload, operation.receipt = "cancelled", {}, {"status": "owner_stopped"}
            return deepcopy(self.value)
        self.fetch.side_effect = cancel
        result = self.advance(operation)
        self.assertEqual((result.state, result.payload, result.receipt), ("cancelled", {}, {"status": "owner_stopped"}))

    def test_transient_read_has_bounded_backoff_and_never_relaunches_generation(self):
        operation = self.start()
        self.fetch.side_effect = review.ArtifactReadError("unavailable")
        current = self.now
        for attempt in range(1, 6):
            result = review.advance_review(operation.pk, now=current)
            self.assertEqual(result.attempts, attempt)
            self.assertEqual(result.payload["failures"], attempt)
            if attempt < 5:
                self.assertEqual(result.state, "pending")
                self.assertGreater(result.next_attempt_at, current)
                current = result.next_attempt_at + timedelta(seconds=1)
            else:
                self.assertEqual(result.state, "blocked")
                self.assertIsNone(result.next_attempt_at)
        self.assertEqual(self.fetch.call_count, 5)
        self.assertEqual(self.parent.state, "pending")

    def test_dead_worker_lease_is_reclaimed_after_deadline_without_accepting_old_results(self):
        operation = self.start()
        self.fetch.side_effect = RuntimeError("synthetic worker crash")
        with self.assertRaises(RuntimeError): self.advance(operation)
        old_lease = operation.payload["lease_id"]
        self.assertEqual(operation.state, "running")
        self.assertIs(self.advance(operation), operation)
        self.assertEqual(self.fetch.call_count, 1)
        self.fetch.side_effect = None
        result = self.advance(operation, now=operation.next_attempt_at + timedelta(seconds=1))
        self.assertEqual((result.state, result.attempts), ("completed", 2))
        self.assertNotIn("lease_id", result.payload)
        self.assertTrue(old_lease)

    def test_mismatched_revision_run_generation_and_format_fail_closed(self):
        for changes in ({"run_id": "other-run"}, {"run_generation": 3}, {"review_revision": "f" * 64}):
            operation = self.start(expected_revision=self.value["review_revision"])
            self.fetch.return_value = seal({**deepcopy(self.value), **changes})
            result = self.advance(operation)
            self.assertEqual(result.state, "cancelled")
            self.assertNotIn("handoff", result.payload)
            self.saved.remove(operation)


class CmsArtifactSurfaceTests(SimpleTestCase):
    def test_private_artifact_collection_is_not_exposed_by_owner_operation_detail(self):
        from . import website_views
        context = SimpleNamespace(organization=SimpleNamespace(pk=7))
        with patch.object(website_views, "_context", return_value=(context, None, None)), \
                patch.object(WebsiteConnectionOperation.objects, "filter") as rows:
            for action in ("cms-review", "cms-media", "cms-stage", "cms-policy", "cms-approval", "cms-publish", "cms-goal", "cms-goal-refund", "cms-setup", "cms-setup-intent", "cms-setup-refund", "cms-credential-renewal", "cms-initial-connect", "cms-initial-grant"):
                rows.return_value.first.return_value = SimpleNamespace(action=action, payload={"handoff": "private"})
                response = website_views.WebsiteConnectionOperationView().get(SimpleNamespace(), "private-operation")
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("private", json.dumps(response.data))

    def test_canonical_fixture_is_valid_and_post_intent_accepts_the_same_native_aside(self):
        value = fixture()
        self.assertEqual(review.validate_review_surface(value), value)
        from .tests_cms_post_contract_unit import intent
        html = native_fragment('<article><h1>Approved native article</h1><aside><p>A useful note.</p></aside></article>', title="Approved native article")
        self.assertEqual(intent(html=html)["html"], html)

    def test_preview_cannot_hide_replace_or_rewrite_the_approved_body_or_headline(self):
        value = fixture()
        for preview in (value["preview_html"].replace("concrete", "different"), value["preview_html"] + "<script>private()</script>",
                value["preview_html"].replace("<article ", '<article style="display:none" '),
                value["preview_html"].replace("<article ", '<article class="first" class="second" '),
                value["preview_html"].replace("Fixture guide", "Another guide"),
                value["preview_html"].replace("</h1>", '</h1><img src="https://private.test/image.png">'),
                value["preview_html"].replace("<h2>", '<h2 onclick="private()">')):
            with self.subTest(preview=preview[:25]), self.assertRaises(CmsArtifactError):
                review.validate_review_surface(seal({**deepcopy(value), "preview_html": preview}))
        value["article"]["html"] = native_fragment(value["article"]["html"].replace("<article ", '<article aria-hidden="true" '), title=value["article"]["title"])
        value["article"]["profile"] = profile_receipt(value["article"]["html"], title=value["article"]["title"])
        value["editorial_acceptance"]["transport_sha256"] = value["article"]["profile"]["html_sha256"]
        with self.assertRaises(CmsArtifactError): review.validate_review_surface(seal(value))

    def test_code_whitespace_and_image_inventory_are_exact_in_the_review(self):
        value = fixture()
        title = value["article"]["title"]
        fragment = native_fragment('<article><pre><code>A\tB\n1\t2\n</code></pre><p><a href="/safe">Exact source.</a></p></article>', title=title)
        value["article"].update(html=fragment, profile=profile_receipt(fragment, title=title))
        value["editorial_acceptance"]["transport_sha256"] = value["article"]["profile"]["html_sha256"]
        value["preview_html"] = preview_document(fragment, title=title)
        self.assertEqual(review.validate_review_surface(seal(value)), value)
        original_preview = value["preview_html"]
        value["preview_html"] = original_preview.replace('href="/safe"', 'href="javascript:private()" href="/safe"')
        with self.assertRaises(CmsArtifactError): review.validate_review_surface(seal(value))
        value["preview_html"] = original_preview
        value["preview_html"] = value["preview_html"].replace("A\tB", "A B")
        with self.assertRaises(CmsArtifactError): review.validate_review_surface(seal(value))

    @override_settings(CONTENT_FACTORY_URL="https://cf.example.test", CONTENT_FACTORY_API_KEY="synthetic-service-key")
    def test_fetch_stream_is_bounded_authenticated_and_never_follows_redirects(self):
        value = fixture()
        response = Mock(status_code=200, headers={"Content-Type": "application/json"})
        response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [json.dumps(value).encode()]
        with patch.object(review.requests, "get", return_value=response) as get:
            result = review.fetch_handoff(SimpleNamespace(run_id=value["run_id"]), expected_revision=value["review_revision"])
        self.assertEqual(result, value)
        self.assertIs(get.call_args.kwargs["allow_redirects"], False)
        self.assertIs(get.call_args.kwargs["stream"], True)
        self.assertEqual(get.call_args.kwargs["headers"]["X-API-KEY"], "synthetic-service-key")
        self.assertEqual(get.call_args.kwargs["params"], {"expected_revision": value["review_revision"]})
        for status, category in ((302, "changed"), (409, "changed"), (429, "unavailable"), (503, "unavailable")):
            response.status_code = status
            with patch.object(review.requests, "get", return_value=response), self.assertRaises(review.ArtifactReadError) as error:
                review.fetch_handoff(SimpleNamespace(run_id=value["run_id"]))
            self.assertEqual(error.exception.category, category)

    @override_settings(CONTENT_FACTORY_URL="https://cf.example.test", CONTENT_FACTORY_API_KEY="synthetic-service-key")
    def test_oversize_duplicate_json_compressed_and_malformed_replies_do_not_leak_source(self):
        response = Mock(status_code=200, headers={"Content-Type": "application/json"})
        response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
        for headers, content in (({"Content-Length": "99999999"}, b"private-source"),
                ({"Content-Encoding": "gzip"}, b"private-source"), ({}, b'{"private-source":1,"private-source":2}'),
                ({}, b"x" * (review.MAX_HANDOFF_BYTES + 1))):
            response.headers = {"Content-Type": "application/json", **headers}
            response.iter_content.return_value = [content]
            with patch.object(review.requests, "get", return_value=response), self.assertRaises(review.ArtifactReadError) as error:
                review.fetch_handoff(SimpleNamespace(run_id="native-run"))
            self.assertNotIn("private-source", str(error.exception))
            self.assertEqual(error.exception.category, "invalid")

    def test_exact_producer_lock_covers_native_renderer_handoff_schemas_and_examples(self):
        from scripts.check_cms_artifact_contract import verify
        from scripts.check_failure_contract import ContractCheckError
        lock = verify()
        self.assertEqual(lock["source_revision"], "3c566b5c8f09451806c276371c8e3e0192ef6503")
        self.assertEqual(len(lock["files"]), 6)
        source = {row["source_path"]: (ROOT / row["local_path"]).read_bytes() for row in lock["files"]}
        self.assertEqual(verify(remote=True, token="synthetic-read-key", fetch=lambda path, revision, token: source[path]), lock)
        self.assertEqual(validate_handoff(fixture()), fixture())
        with self.assertRaises(ContractCheckError): verify(remote=True, token=None)
        with self.assertRaises(ContractCheckError): verify(remote=True, token="synthetic-read-key", fetch=lambda *a: b"changed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for row in lock["files"]:
                destination = root / row["local_path"]; destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source[row["source_path"]])
            path = root / "contracts/cms-article-artifact-v1.lock.json"
            path.write_text(json.dumps(lock))
            self.assertEqual(verify(root), lock)
            lock["files"][0]["source_path"] = "backend/private.py"
            path.write_text(json.dumps(lock))
            with self.assertRaises(ContractCheckError): verify(root)
