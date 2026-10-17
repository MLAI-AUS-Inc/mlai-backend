"""Original paid CMS artifact intent is separate from Git, exports and approval."""

from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from founder_tools.models import VibeRaisingCompany
from organizations.models import Organization
from . import channel_artifacts as artifacts
from . import tests_cms_connection_authority_unit as authority_fixtures
from . import tests_cms_connection_contract_unit as keys
from .cms_operation_scope import current_scope
from .dispatch_models import ContentFactoryDispatchOutbox
from .models import OrganizationContentConfig
from .portable_drafts import original_portable_run
from .tests_cms_capture_unit import Rows
from .website_channel_observation import classify_prefix
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnection, WebsiteConnectionOperation
from .website_publishing_channels import recommend, scope_for


@override_settings(ARTICLES_CHANNEL_DISCOVERY_ENABLED=True, ARTICLES_PUBLISHING_CHANNELS_ENABLED=True,
    ARTICLES_CMS_CHANNEL_ENABLED=True, ARTICLES_CHANNEL_ARTIFACTS_ENABLED=True,
    CONNECTOR_CREDENTIAL_KEYS=keys.json.dumps({"test": keys.TEST_KEY}), CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID="test")
class ChannelArtifactTests(SimpleTestCase):
    def setUp(self):
        authority_fixtures.CmsAuthorityTests.setUp(self)
        self.company.profile = self.profile
        for value in (self.org, self.company, self.profile, self.user, self.config):
            value.refresh_from_db = Mock()
        self.config.article_system = {"publishing_channel": {"schema_version": 1, "revision": 3,
            "scope": scope_for(self.context, self.config), "selection": {"channel": "cms", "platform": "wordpress"},
            "recommendation": recommend(classify_prefix(b'<head><meta name="generator" content="WordPress"></head>')),
            "observed_at": timezone.now().isoformat()}}
        self.saved = []
        self.manager = Rows(self.saved)
        self.outbox = SimpleNamespace(organization_id=7, state="pending", payload={})
        self.patches = [patch("django.db.transaction.atomic", side_effect=lambda: nullcontext()),
            patch("django.db.transaction.get_connection", return_value=SimpleNamespace(in_atomic_block=True)),
            patch.object(Organization.objects, "select_for_update"), patch.object(WebsiteConnection.objects, "select_for_update"),
            patch.object(WebsiteConnectionOperation.objects, "filter", side_effect=self.manager.filter),
            patch.object(WebsiteConnectionOperation.objects, "get_or_create", side_effect=self.manager.get_or_create),
            patch.object(OrganizationContentConfig.objects, "select_related"),
            patch.object(VibeRaisingCompany.objects, "select_related"), patch.object(get_user_model().objects, "filter"),
            patch.object(ContentFactoryDispatchOutbox.objects, "filter")]
        values = [item.start() for item in self.patches]
        values[6].return_value.filter.return_value.first.return_value = self.config
        values[7].return_value.filter.return_value.first.return_value = self.company
        values[8].return_value.first.return_value = self.user
        values[9].return_value.first.return_value = self.outbox
        for item in self.patches: self.addCleanup(item.stop)
        self.payload = {"domain": "fixture.test", "client_request_id": "cms-paid:" + uuid4().hex,
            "delivery_mode": "content_only", "delivery_mode_confirmed": True, "github_repo": "",
            "topic": "Current approved topic", "target_keyword": "Current approved topic",
            "editorial_brief": {"audience": "Approved audience"}}

    def reserve(self):
        scope = artifacts.prepare_generation(self.context, self.config, user=self.user)
        operation = artifacts.reserve_generation(scope, context=self.context, config=self.config, user=self.user, payload=self.payload)
        self.payload.update(roo_points_ledger_id="original-spend", roo_points_billing_user_id=4, roo_points_requested_by_user_id=4)
        self.outbox.payload = {"request": deepcopy(self.payload)}
        return operation

    def artifact_run(self):
        return SimpleNamespace(run_id="original-run", organization_id=7, workflow="article_generation", domain="fixture.test",
            github_repo="", status="running", run_request=deepcopy(self.payload))

    def test_reservation_reuses_original_key_and_contains_no_credential_or_approval(self):
        operation = self.reserve()
        again = self.reserve()
        self.assertIs(operation, again)
        self.assertEqual(len(self.saved), 1)
        value = artifacts.authorize_payload(self.payload)
        self.assertEqual(value.operation.pk, operation.pk)
        self.assertEqual(value.envelope["review_policy"], "review")
        self.assertNotIn("api_origin", value.envelope)
        for forbidden in ("synthetic-application-password", "sealed_credential", "approved"):
            self.assertNotIn(forbidden, keys.json.dumps(operation.payload))
        self.assertFalse(original_portable_run(self.artifact_run()))

    def test_private_controller_and_original_transaction_are_required(self):
        scope = artifacts.prepare_generation(self.context, self.config, user=self.user)
        for supplied in (None, scope.__dict__, {"approved": True}):
            with self.subTest(supplied=type(supplied)), self.assertRaises(WebsiteAuthorityError):
                artifacts.reserve_generation(supplied, context=self.context, config=self.config, user=self.user, payload=self.payload)
        with patch("django.db.transaction.get_connection", return_value=SimpleNamespace(in_atomic_block=False)), self.assertRaises(WebsiteAuthorityError):
            artifacts.reserve_generation(scope, context=self.context, config=self.config, user=self.user, payload=self.payload)
        self.assertEqual(self.saved, [])

    def test_changed_owner_generation_connection_and_selection_deny_original_request(self):
        operation = self.reserve()
        for target, key, value in ((self.user, "is_active", False), (self.company, "profile_id", 99),
                (self.connection, "generation", 2), (self.connection, "state", "disconnected"),
                (self.connection, "authorized_by_id", 9), (self.config, "auto_publish", True)):
            old = getattr(target, key)
            setattr(target, key, value)
            with self.subTest(key=key), self.assertRaises(WebsiteAuthorityError): artifacts.authorize_payload(self.payload)
            setattr(target, key, old)
        self.config.article_system["publishing_channel"]["selection"]["channel"] = "portable"
        with self.assertRaises(WebsiteAuthorityError): artifacts.authorize_payload(self.payload)
        self.assertEqual(operation.state, "pending")

    def test_worker_cannot_rebind_content_policy_billing_or_repository_fields(self):
        self.reserve()
        for changes in ({"topic": "Other topic"}, {"editorial_brief": {}}, {"client_request_id": "other"},
                {"roo_points_ledger_id": "other-spend"}, {"roo_points_requested_by_user_id": 9},
                {"github_repo": "customer/site"}, {"website_connection_id": str(uuid4())}, {"delivery_mode": "publish_code"}):
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError):
                artifacts.authorize_payload({**self.payload, **changes})
        envelope = self.payload[artifacts.ENVELOPE]
        with self.assertRaises(WebsiteAuthorityError):
            artifacts.authorize_payload({**self.payload, artifacts.ENVELOPE: {**envelope, "approved": True}})

    def test_missing_refunded_cancelled_or_erased_original_reservation_denies_worker(self):
        operation = self.reserve()
        for state in ("failed", "cancelled", "refund_pending"):
            self.outbox.state = state
            with self.subTest(state=state), self.assertRaises(WebsiteAuthorityError): artifacts.authorize_payload(self.payload)
        self.outbox.state = "pending"
        for state in ("cancelled", "deleted", "completed"):
            operation.state = state
            with self.subTest(state=state), self.assertRaises(WebsiteAuthorityError): artifacts.authorize_payload(self.payload)
        operation.state = "pending"
        self.private.state = "deleted"
        with self.assertRaises(WebsiteAuthorityError): artifacts.authorize_payload(self.payload)

    def test_disabled_artifacts_do_not_read_private_rows(self):
        with override_settings(ARTICLES_CHANNEL_ARTIFACTS_ENABLED=False), \
                patch.object(WebsiteConnectionOperation.objects, "filter") as rows, self.assertRaises(WebsiteAuthorityError):
            artifacts.authorize_payload(self.payload)
        rows.assert_not_called()

    def test_admission_is_exact_minimal_original_outbox_scope_and_cancelled_run_denies_it(self):
        self.reserve()
        envelope = self.payload[artifacts.ENVELOPE]
        query = {"artifact_operation_id": envelope["artifact_operation_id"], "envelope_digest": envelope["digest"],
            "billing_request_id": envelope["billing_request_id"], "domain": "fixture.test"}
        from workflow_runs.models import ContentFactoryRun
        with patch.object(ContentFactoryRun.objects, "filter") as runs:
            runs.return_value.filter.return_value.exists.return_value = False
            receipt = artifacts.admission_receipt(query)
            self.assertEqual(receipt, {"allowed": True, "permission_mode": "none", "capabilities": {}, artifacts.ENVELOPE: envelope})
            for key in ("artifact_operation_id", "envelope_digest", "domain"):
                with self.subTest(key=key), self.assertRaises(WebsiteAuthorityError): artifacts.admission_receipt({**query, key: "other"})
            runs.return_value.filter.return_value.exists.return_value = True
            with self.assertRaises(WebsiteAuthorityError): artifacts.admission_receipt(query)

    def test_artifact_callback_allows_content_but_never_worker_approval_or_provider_proof(self):
        self.reserve()
        run = self.artifact_run()
        self.assertTrue(artifacts.artifact_run_update_allowed(run, {"status": "completed", "result": {"content_package": {"article_html": "<p>Body</p>"}}}))
        for payload in ({"status": "published"}, {"approval_state": "approved"}, {"approved": True},
                {"result": {"route_is_live": True}}, {"run_request": {"github_repo": "customer/site"}},
                {"result": {"cms_publish_approval": {"approved": True}}}, {"result": {"application_password": "private"}},
                {"run_request": {artifacts.ENVELOPE: None}}, {"domain": "foreign.test"}, {"job_id": "foreign-run"}):
            with self.subTest(fields=tuple(payload)):
                self.assertFalse(artifacts.artifact_run_update_allowed(run, payload))
        self.connection.generation = 2
        self.assertFalse(artifacts.artifact_run_update_allowed(run, {"status": "completed"}))

    def test_sparse_snapshots_preserve_exact_envelope_and_worker_cannot_enroll_it(self):
        self.reserve()
        from .service_views import _merge_django_owned_run_request
        from .editorial_run_state import EditorialRunConflict
        original = {artifacts.ENVELOPE: self.payload[artifacts.ENVELOPE]}
        self.assertEqual(_merge_django_owned_run_request(original, {}), original)
        self.assertEqual(_merge_django_owned_run_request({}, original), {})
        with self.assertRaises(EditorialRunConflict): _merge_django_owned_run_request(original, {artifacts.ENVELOPE: None})

    def test_service_callback_guard_rechecks_current_private_scope_before_any_write(self):
        self.reserve()
        run = self.artifact_run()
        from workflow_runs.models import ContentFactoryRun
        from .website_connections import guarded_service_write
        handler = Mock(return_value="accepted")
        guarded = guarded_service_write("config_write", portable=True, only_repository=True)(handler)
        request = SimpleNamespace(method="PUT", data={"status": "completed"})
        with patch.object(ContentFactoryRun.objects, "select_for_update") as runs:
            runs.return_value.filter.return_value.first.return_value = run
            with patch.object(ContentFactoryRun.objects, "filter") as candidate:
                candidate.return_value.first.return_value = run
                self.assertEqual(guarded(None, request, run_id=run.run_id), "accepted")
                handler.reset_mock()
                self.connection.state = "disconnected"
                response = guarded(None, request, run_id=run.run_id)
                self.assertEqual(response.status_code, 409)
                handler.assert_not_called()

    def test_cms_controller_enrolls_private_scope_but_keeps_original_generation_checks(self):
        from .publishing_channel_adapters import CmsChannel
        from .publishing_channel_contract import ChannelRequest
        from . import vibe_marketing_views as views
        request = ChannelRequest(context=self.context, config=self.config, user=self.user,
            step="generation", data={"data": {"topic": "Owner selected topic"}})
        with patch.object(views, "_start_article_generation_for_channel", return_value="canonical") as start:
            self.assertEqual(CmsChannel().publish(request), "canonical")
        supplied = start.call_args.kwargs
        self.assertIsInstance(supplied["channel_artifact_scope"], artifacts.GenerationScope)
        self.assertEqual(supplied["data"], {"topic": "Owner selected topic", "delivery_mode": "content_only",
            "delivery_mode_explicit": True, "delivery_mode_confirmed": True})
        self.assertNotIn(artifacts.ENVELOPE, supplied["data"])

    def test_artifact_poll_and_resume_reuse_private_scope_without_git_permission(self):
        self.reserve()
        run = self.artifact_run()
        run.pk, run.result, run.refresh_from_db = 1, {}, Mock()
        from . import vibe_marketing_views as views
        response = Mock(status_code=202, content=b'{}')
        response.json.return_value = {"status": "queued", "run_id": run.run_id}
        with patch.object(views, "authority_guard", side_effect=AssertionError("No Git authority")), \
                patch.object(views.ContentFactoryRun.objects, "select_for_update") as locked, \
                patch.object(views.ContentFactoryRun.objects, "filter") as runs, \
                patch.object(views, "_sync_local_run_from_remote_locked", return_value=run) as sync, \
                patch.object(views, "require_unlocked_remote_call"), \
                patch.object(views, "_content_factory_remote_config", return_value={"enabled": True, "base_url": "https://factory.test"}), \
                patch.object(views, "_content_factory_headers", return_value={}), \
                patch.object(views.http_client, "post", return_value=response) as post:
            locked.return_value.get.return_value = run
            runs.return_value.first.return_value = run
            self.assertIs(views._sync_local_run_from_remote(run, {"status": "completed"}), run)
            sync.assert_called_once()
            self.assertEqual(views._call_content_factory_run_action(run_id=run.run_id, action="resume", payload={})["status"], "queued")
            self.assertEqual(post.call_count, 1)
            result = views._call_content_factory_run_action(run_id=run.run_id, action="publish-pr", payload={})
            self.assertEqual(result["content_factory_status_code"], 409)
            self.assertEqual(post.call_count, 1)

    def test_late_accepted_dispatch_after_disconnect_is_cancelled_under_original_company(self):
        self.reserve()
        from . import vibe_marketing_views as views
        run = self.artifact_run()
        run.status = "cancelled"
        self.connection.state = "disconnected"
        with patch.object(WebsiteConnection.objects, "filter") as websites, \
                patch.object(views.ContentFactoryRun.objects, "get_or_create", return_value=(run, True)) as create, \
                patch.object(views, "_create_local_run_authorized", side_effect=AssertionError("No successful projection")):
            websites.return_value.first.return_value = self.connection
            WebsiteConnection.objects.select_for_update.return_value.get.return_value = self.connection
            result = views._create_local_run(workflow="article_generation", domain="fixture.test", payload=self.payload,
                remote_data={"run_id": "late-remote-run", "status": "queued"})
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(create.call_args.kwargs["defaults"]["organization"], self.org)
        cleanup = next(row for row in self.saved if row.action == "disconnect")
        self.assertEqual(cleanup.payload, {"cancel_run_ids": ["late-remote-run"], "stop_preview_run_ids": ["late-remote-run"], "previous_generation": 1})

    def test_canonical_charge_enlists_artifact_then_spend_then_outbox_and_rolls_back_on_refusal(self):
        from . import vibe_marketing_views as views
        from . import dispatch_outbox
        events = []
        scope = artifacts.prepare_generation(self.context, self.config, user=self.user)
        request = SimpleNamespace(user=self.user, data={"client_request_id": self.payload["client_request_id"]})
        def charge(**kwargs):
            events.append("spend")
            self.assertIn(artifacts.ENVELOPE, kwargs["article_request"])
            return self.user, SimpleNamespace(id="synthetic-ledger"), 6
        with patch.object(views, "_quoted_price_response", return_value=None), \
                patch.object(views, "_refresh_article_editorial_payload", return_value=None), \
                patch.object(views, "_reserve_article_start_connection", return_value=None), \
                patch.object(views, "_validate_article_start_admission"), patch.object(views, "_get_config", return_value=self.config), \
                patch.object(views, "founder_actor_id_for_user", return_value="owner-actor"), \
                patch.object(views, "_roo_points_balance_for_user", return_value=20), \
                patch.object(views, "build_roo_points_authorization_payload", return_value={"roo_points_ledger_id": "synthetic-ledger"}), \
                patch.object(views, "charge_content_factory_request_for_user", side_effect=charge) as spender, \
                patch.object(artifacts, "reserve_generation") as reservation, \
                patch.object(dispatch_outbox, "reserve_dispatch", side_effect=lambda **kw: events.append("outbox")), \
                patch("django.db.transaction.set_rollback") as rollback:
            # The real reservation is covered separately; this seam asserts the
            # canonical controller's ordering, payload retention and rollback.
            reservation.side_effect = lambda *a, **kw: (events.append("artifact"), self.payload.update(
                publishing_channel_artifact={"original": True}))
            result = views._charge_roo_points_for_article(request, context=self.context,
                payload=self.payload, channel_artifact_scope=scope)
            self.assertIsNone(result[-1])
            self.assertEqual(events, ["artifact", "spend", "outbox"])
            rollback.assert_not_called()
            events.clear()
            reservation.side_effect = WebsiteAuthorityError("website_connection_changed", "Revoked")
            spender.reset_mock()
            result = views._charge_roo_points_for_article(request, context=self.context,
                payload=self.payload, channel_artifact_scope=scope)
            self.assertEqual(result[-1].status_code, 409)
            rollback.assert_called_once_with(True)
            spender.assert_not_called()


class ChannelArtifactParityTests(SimpleTestCase):
    def test_vendored_validator_schema_and_fixture_match_one_immutable_producer(self):
        from scripts.check_channel_artifact_contract import verify, ROOT
        from scripts.check_failure_contract import ContractCheckError
        from .channel_artifact_contract import validate_envelope
        lock = verify()
        self.assertEqual(lock["source_revision"], "979da46e05a63039192806cd60862951f49ff542")
        fixture = json.loads((ROOT / "contracts/channel-artifact-envelope-v1.example.json").read_text())
        self.assertEqual(validate_envelope(fixture), fixture)
        by_path = {row["source_path"]: (ROOT / row["local_path"]).read_bytes() for row in lock["files"]}
        calls = []
        def fetch(path, revision, token):
            calls.append((path, revision, token))
            return by_path[path]
        self.assertEqual(verify(remote=True, token="synthetic-read-token", fetch=fetch), lock)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(row[1:] == (lock["source_revision"], "synthetic-read-token") for row in calls))
        with self.assertRaises(ContractCheckError): verify(remote=True, token=None, fetch=fetch)
        with self.assertRaises(ContractCheckError): verify(remote=True, token="synthetic-read-token", fetch=lambda *a: b"changed")

    def test_local_tampering_or_a_nonallowlisted_source_cannot_pass_the_lock(self):
        from scripts.check_channel_artifact_contract import verify, ROOT
        from scripts.check_failure_contract import ContractCheckError
        lock = verify()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for row in lock["files"]:
                destination = root / row["local_path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((ROOT / row["local_path"]).read_bytes())
            target = root / "contracts/channel-artifact-v1.lock.json"
            target.write_text(json.dumps(lock))
            self.assertEqual(verify(root), lock)
            changed = root / lock["files"][0]["local_path"]
            changed.write_bytes(b"changed")
            with self.assertRaises(ContractCheckError): verify(root)
            changed.write_bytes((ROOT / lock["files"][0]["local_path"]).read_bytes())
            lock["files"][0]["source_path"] = "backend/private.py"
            target.write_text(json.dumps(lock))
            with self.assertRaises(ContractCheckError): verify(root)
