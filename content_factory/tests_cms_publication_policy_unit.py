"""Only current private owner consent selects native auto publication."""

from contextlib import contextmanager
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from . import cms_publication_policy as policies
from . import channel_artifacts
from . import tests_channel_artifacts_unit as fixtures
from . import service_views as _service_views  # load transaction decorators before fixture mocks
from . import vibe_marketing_views as _views
from . import article_review_views as _review_views
from .cms_api_transport import CmsApiError
from .tests_cms_connection_contract_unit import TEST_KEY
from .website_connections import contract_for
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnectionOperation


@override_settings(ARTICLES_CHANNEL_DISCOVERY_ENABLED=True, ARTICLES_PUBLISHING_CHANNELS_ENABLED=True,
    ARTICLES_CMS_CHANNEL_ENABLED=True, ARTICLES_CHANNEL_ARTIFACTS_ENABLED=True,
    CONNECTOR_CREDENTIAL_KEYS=json.dumps({"test": TEST_KEY}), CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID="test")
class CmsPublicationPolicyTests(SimpleTestCase):
    reserve = fixtures.ChannelArtifactTests.reserve

    def setUp(self):
        fixtures.ChannelArtifactTests.setUp(self)
        self.connection.save = Mock()
        self.data = {**contract_for(self.connection), "client_request_id": str(uuid4()), "review_policy": "auto",
            "publication_confirmed": True, "native_defaults_confirmed": True, "expected_policy_id": None}
        self.depth = 0
        @contextmanager
        def atomic(*args, **kwargs):
            self.depth += 1
            try: yield
            finally: self.depth -= 1
        def response(binding, credential, **options):
            self.assertEqual(self.depth, 0)
            self.assertEqual(binding, self.binding)
            self.assertEqual(options["method"], "GET")
            if options["resource"] == "users":
                return {"id": 7, "name": "Existing author", "capabilities": {"edit_posts": True, "publish_posts": True}}, {}
            if options["resource"] == "categories":
                return [{"id": 39, "taxonomy": "category", "name": "Existing category"}], {}
            raise AssertionError("Unexpected native discovery")
        self.extra = [patch("django.db.transaction.atomic", side_effect=atomic),
            patch.object(policies, "request_json", side_effect=response),
            patch("content_factory.website_connections.require_unlocked_remote_call", side_effect=lambda: self.assertEqual(self.depth, 0))]
        values = [item.start() for item in self.extra]
        self.http = values[1]
        for item in self.extra: self.addCleanup(item.stop)
        def relation(**filters):
            if filters.get("action") == "channel-credential": return SimpleNamespace(first=lambda: self.private)
            return self.manager.filter(**filters)
        self.rows.filter.side_effect = relation
        self.rows.exclude.return_value.only.return_value.order_by.return_value.first.return_value = None

    def configure(self, **changes):
        return policies.configure(self.context, self.config, user=self.user, data={**self.data, **changes})

    def test_explicit_owner_auto_choice_reads_real_ids_and_is_idempotent_without_more_http(self):
        value = self.configure()
        policy = value["policy"]
        self.assertEqual(policy["native_template"], {"author": 7, "categories": [39], "tags": [], "hero": "inline_only"})
        self.assertEqual(policy["review_policy"], "auto")
        self.assertEqual(self.configure(), value)
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(self.http.call_args.kwargs["query"], {"per_page": 1, "orderby": "id", "order": "asc", "hide_empty": "false"})
        self.assertNotIn("synthetic-application-password", json.dumps(policy))
        projected = policies.projection(value)
        self.assertEqual(projected["category"], "Existing category")
        self.assertNotIn("api_origin", projected)
        self.assertNotIn("credential_operation_id", projected)
        self.assertFalse(self.connection.capabilities.get("publishingReady", False))

    def test_valid_native_identity_without_publishing_permission_needs_owner_access_before_category_discovery(self):
        for capabilities in ({"edit_posts": True, "publish_posts": False}, {"edit_posts": False, "publish_posts": True}, {}):
            with self.subTest(capabilities=capabilities):
                self.http.side_effect = None
                self.http.return_value = ({"id": 7, "capabilities": capabilities}, {})
                self.http.reset_mock()
                with self.assertRaises(WebsiteAuthorityError) as failure:
                    self.configure()
                self.assertEqual(failure.exception.authority_code, "missing_or_expired_credentials")
                self.http.assert_called_once()
                self.assertFalse(any(row.action == policies.ACTION for row in self.saved))

    def test_manual_choice_stays_manual_and_neither_git_preference_nor_worker_approval_grants_cms_auto(self):
        self.config.auto_publish = True
        legacy = channel_artifacts.prepare_generation(self.context, self.config, user=self.user)
        self.assertEqual(legacy.policy["review_policy"], "review")
        saved = self.configure(review_policy="review")
        scope = channel_artifacts.prepare_generation(self.context, self.config, user=self.user)
        self.assertEqual(scope.policy["review_policy"], "review")
        self.assertEqual(scope.policy["cms_publication_policy"], {"operation_id": saved["operation_id"], "digest": saved["policy"]["digest"]})
        for changes in ({"publication_confirmed": 1}, {"native_defaults_confirmed": False}, {"review_policy": "approved"}):
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): self.configure(**changes)

    def test_duplicate_request_winning_during_provider_reads_reuses_the_same_owner_choice(self):
        original = self.http.side_effect
        winner = []
        def duplicate(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs["resource"] == "categories":
                self.http.side_effect = original
                winner.append(self.configure())
            return result
        self.http.side_effect = duplicate
        self.assertEqual(self.configure(), winner[0])
        self.assertEqual(len([row for row in self.saved if row.action == policies.ACTION]), 1)
        self.assertEqual(self.connection.save.call_count, 1)

    def test_native_consent_is_sealed_into_original_artifact_envelope_before_generation(self):
        saved = self.configure()
        operation = self.reserve()
        self.assertEqual(self.payload["publishing_channel_artifact"]["review_policy"], "auto")
        admitted = channel_artifacts.authorize_payload(self.payload)
        self.assertEqual(admitted.operation.pk, operation.pk)
        self.assertEqual(admitted.operation.payload["policy"]["cms_publication_policy"]["digest"], saved["policy"]["digest"])
        worker_run = fixtures.ChannelArtifactTests.artifact_run(self)
        self.assertFalse(channel_artifacts.artifact_run_update_allowed(worker_run, {"approved": True}))

    def test_article_publication_uses_only_the_native_policy_reserved_before_its_paid_run(self):
        saved = self.configure()
        self.reserve()
        admitted = channel_artifacts.authorize_payload(self.payload)
        source = SimpleNamespace(admitted=admitted, handoff={"envelope": admitted.envelope})
        self.assertEqual(policies.for_article(source), saved)
        admitted.operation.payload["policy"].pop("cms_publication_policy")
        with self.assertRaises(WebsiteAuthorityError): policies.for_article(source)

    def test_owner_route_returns_only_current_choices_and_no_provider_access_or_approval(self):
        from . import website_views
        request = SimpleNamespace(user=self.user, data=self.data)
        with patch.object(website_views, "_context", return_value=(self.context, self.config, None)):
            response = website_views.WebsiteConnectionActionView().post(request, "cms-configure")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["cmsPublicationPolicy"]["reviewPolicy"], "auto")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        for private in ("api_origin", "credential_operation_id", "synthetic-application-password", "approved"):
            self.assertNotIn(private, json.dumps(response.data))

    def test_changing_native_policy_invalidates_old_paid_scope_and_stale_requests_cannot_restore_it(self):
        first = self.configure()
        self.reserve()
        newer = self.configure(client_request_id=str(uuid4()), review_policy="review", expected_policy_id=first["operation_id"])
        self.assertNotEqual(first["operation_id"], newer["operation_id"])
        with self.assertRaises(WebsiteAuthorityError): channel_artifacts.authorize_payload(self.payload)
        with self.assertRaises(WebsiteAuthorityError): self.configure()

    def test_changed_owner_generation_credential_or_connection_is_denied_before_provider_reads(self):
        for target, key, changed in ((self.user, "is_active", False), (self.connection, "generation", 2),
                (self.profile, "role", "investor"), (self.connection, "state", "disconnected")):
            before = getattr(target, key); setattr(target, key, changed)
            with self.subTest(key=key), self.assertRaises(WebsiteAuthorityError): self.configure()
            setattr(target, key, before)
        self.http.assert_not_called()

    def test_disconnect_or_owner_change_during_native_read_cannot_commit_policy(self):
        original = self.http.side_effect
        def revoke(*args, **kwargs):
            result = original(*args, **kwargs)
            self.connection.state = "disconnected"
            return result
        self.http.side_effect = revoke
        with self.assertRaises(WebsiteAuthorityError): self.configure()
        self.assertNotIn(policies.REFERENCE, self.connection.capabilities)
        self.assertEqual([row for row in self.saved if row.action == policies.ACTION], [])

    def test_disabled_policy_does_not_read_credentials_or_provider_metadata(self):
        with override_settings(ARTICLES_CMS_CHANNEL_ENABLED=False), self.assertRaises(WebsiteAuthorityError): self.configure()
        self.http.assert_not_called()
        self.rows.filter.assert_not_called()

    def test_foreign_private_reference_and_rechecksummed_or_boolean_consent_are_not_authority(self):
        saved = self.configure()
        row = next(row for row in self.saved if row.action == policies.ACTION)
        original = deepcopy(row.payload)
        for changes in ({"schema_version": True}, {"publication_confirmed": 1}, {"review_policy": "approved"},
                {"native_template": {**original["native_template"], "author": True}}, {"approved": True}):
            row.payload = {**deepcopy(original), **changes}
            row.payload["digest"] = policies.digest({key: item for key, item in row.payload.items() if key != "digest"})
            with self.assertRaises(WebsiteAuthorityError): policies.current_policy(self.context, self.config, user=self.user)
        row.payload = original
        self.connection.capabilities[policies.REFERENCE] = str(uuid4())
        with self.assertRaises(WebsiteAuthorityError): policies.current_policy(self.context, self.config, user=self.user)

    def test_native_post_fields_keep_the_exact_reviewed_inline_images_without_duplicate_featured_hero(self):
        saved = self.configure()
        self.assertEqual(policies.native_post_fields(saved["policy"]), {"author": 7, "categories": [39], "tags": [], "featured_media": 0})


class CmsNativeDefaultTests(SimpleTestCase):
    def test_ghost_selects_an_actual_active_author_and_omits_private_staff_fields(self):
        read = Mock(return_value={"users": [{"id": "a" * 24, "status": "active", "name": "Existing author", "email": "synthetic-private@example.test"}]})
        template, labels = policies.resolve_native_defaults(read, "ghost")
        self.assertEqual(template, {"authors": ["a" * 24], "tags": [], "hero": "inline_only"})
        self.assertEqual(labels, {"author": "Existing author", "category": ""})
        self.assertNotIn("synthetic-private", json.dumps([template, labels]))

    def test_unsupported_empty_or_changed_native_metadata_is_not_guessed(self):
        for result in ({"users": []}, {"users": [{"id": "1", "status": "active"}]},
                {"users": [{"id": "a" * 24, "status": "inactive"}]}):
            with self.assertRaises(CmsApiError): policies.resolve_native_defaults(Mock(return_value=result), "ghost")
        user = {"id": 7, "capabilities": {"edit_posts": True, "publish_posts": True}}
        for terms in ([], [{"id": True, "taxonomy": "category"}], [{"id": 39, "taxonomy": "post_tag"}]):
            with self.assertRaises(CmsApiError): policies.resolve_native_defaults(Mock(side_effect=[user, terms]), "wordpress")
