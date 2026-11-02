"""Real dispatch-entrypoint ordering with external HTTP replaced at its boundary."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings

from integrations import http_client
from integrations.services.article_generation import ArticleGenerationError, _post_content_factory_queue_request
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun

from .dispatch_admission import reserve_user_dispatch, post_dispatch
from .run_ownership import OWNER_RESULT_KEY, WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY, owner_receipt, prepare_owned_callback
from .tests_run_ownership import FLAGS
from . import vibe_marketing_views as views


def reply(body, status=202):
    response = Mock(status_code=status, text="synthetic", content=b"{}")
    response.json.side_effect = lambda: deepcopy(body)
    return response


@override_settings(**FLAGS)
class DispatchAdmissionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="r3-dispatch@example.test")
        self.org = Organization.objects.create(name="Synthetic dispatch", domain="dispatch-r3.example")
        self.actor = "mlai_user:" + str(self.user.pk)
        self.remote = {"enabled": True, "base_url": "https://worker.invalid", "api_key_configured": True,
                       "is_local_env": False, "url_configured": True}

    def payload(self, **values):
        return {"domain": self.org.domain, "client_request_id": "dispatch-r3-key",
                "slack_user_id": self.actor, **values}

    def assert_at_http(self, kwargs):
        payload = kwargs["json"]
        run = ContentFactoryRun.objects.get(result__run_owner_v1__identity__dispatch_key=payload["client_request_id"])
        self.assertEqual(run.organization_id, self.org.pk)
        self.assertEqual(owner_receipt(run)["identity"]["principal"]["user_id"], str(self.user.pk))
        self.assertEqual(payload[WIRE_REQUEST_KEY], run.run_request[WIRE_REQUEST_KEY])
        self.assertEqual(payload[WIRE_EXECUTION_KEY], run.run_request[WIRE_EXECUTION_KEY])
        # TestCase's enclosing test transaction is expected; there must be no
        # additional atomic dispatch lock around the provider boundary.
        self.assertEqual(len(connection.atomic_blocks), 2)
        return run

    def test_direct_discovery_has_run_before_actual_http_and_binds_before_job_tracking(self):
        observed = []

        def post(*args, **kwargs):
            observed.append(self.assert_at_http(kwargs).pk)
            return reply({"run_id": "direct-discovery-real"})

        with patch("integrations.services.article_generation.http_requests.post", side_effect=post):
            response = _post_content_factory_queue_request("https://worker.invalid/api/runs/discovery",
                payload=self.payload(), headers={}, operation="queue_discovery", domain=self.org.domain)
        self.assertEqual(response.json()["run_id"], "direct-discovery-real")
        self.assertEqual(ContentFactoryRun.objects.get(run_id="direct-discovery-real").pk, observed[0])

    def test_direct_lost_http_unknown_keeps_original_key_and_does_not_create_remote_identity(self):
        sent = []

        def post(*args, **kwargs):
            self.assert_at_http(kwargs)
            sent.append(deepcopy(kwargs["json"]))
            raise http_client.RequestException("synthetic timeout")

        with patch("integrations.services.article_generation.http_requests.post", side_effect=post), \
                patch.object(views, "_lookup_content_factory_dispatch_by_key", return_value=("unknown", {})), \
                patch.object(views, "_content_factory_remote_config", return_value=self.remote):
            response = _post_content_factory_queue_request("https://worker.invalid/api/runs/discovery",
                payload=self.payload(), headers={}, operation="queue_discovery", domain=self.org.domain)
        body = response.json()
        self.assertTrue(body["dispatch_pending_resolution"])
        self.assertEqual(body["run_id"], "dispatch-r3-key")
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0], sent[1])
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        self.assertEqual(ContentFactoryRun.objects.get().status, "blocked")

    def test_202_without_id_unknown_lookup_holds_same_admission(self):
        with patch("integrations.services.article_generation.http_requests.post", return_value=reply({})), \
                patch.object(views, "_lookup_content_factory_dispatch_by_key", return_value=("unknown", {})), \
                patch.object(views, "_content_factory_remote_config", return_value=self.remote):
            response = _post_content_factory_queue_request("https://worker.invalid/api/runs/discovery",
                payload=self.payload(), headers={}, operation="queue_discovery", domain=self.org.domain)
        self.assertEqual(response.json()["local_dispatch_key"], "dispatch-r3-key")
        self.assertTrue(response.json()["dispatch_pending_resolution"])
        self.assertEqual(ContentFactoryRun.objects.count(), 1)

    def test_lost_ack_lookup_only_adopts_matching_declared_original_key(self):
        for declared, expected in (("foreign", "dispatch-r3-key"), ("dispatch-r3-key", "lookup-real")):
            with self.subTest(declared=declared), \
                    patch("integrations.services.article_generation.http_requests.post", return_value=reply({})), \
                    patch.object(views, "_lookup_content_factory_dispatch_by_key", return_value=("started", {
                        "run_id": "lookup-real", "client_request_id": declared})), \
                    patch.object(views, "_content_factory_remote_config", return_value=self.remote):
                response = _post_content_factory_queue_request("https://worker.invalid/api/runs/discovery",
                    payload=self.payload(), headers={}, operation="queue_discovery", domain=self.org.domain)
            self.assertEqual(response.json()["run_id"], expected)
        self.assertEqual(ContentFactoryRun.objects.count(), 1)

    def test_generic_queue_callback_first_retains_canonical_row_and_fresher_status(self):
        seen = []

        def post(*args, **kwargs):
            original = self.assert_at_http(kwargs)
            callback = {**deepcopy(kwargs["json"]), "workflow": "auto_discovery", "generation": 0,
                        "state_version": 2, "run_id": "generic-callback-real"}
            bound = prepare_owned_callback(callback, "generic-callback-real")
            ContentFactoryRun.objects.filter(pk=bound.pk).update(status="running", current_step="research")
            seen.append((original.pk, original.updated_at))
            return reply({"run_id": "generic-callback-real", "status": "queued"})

        context = SimpleNamespace(organization=self.org, profile=SimpleNamespace(user=self.user))
        config = SimpleNamespace(github_repo="")
        with patch.object(views, "_content_factory_remote_config", return_value=self.remote), \
                patch.object(views, "_content_factory_headers", return_value={}), \
                patch.object(views.http_client, "post", side_effect=post):
            run = views._queue_content_factory_run(endpoint="discovery", workflow="auto_discovery",
                context=context, config=config, payload=self.payload())
        self.assertEqual(run.pk, seen[0][0])
        self.assertEqual(run.updated_at, seen[0][1])
        self.assertEqual(run.status, "running")
        self.assertEqual(run.current_step, "research")
        self.assertEqual(ContentFactoryRun.objects.count(), 1)

    def test_generic_202_unknown_keeps_pending_key_outside_original_request(self):
        payload = self.payload()
        original = deepcopy(payload)
        context = SimpleNamespace(organization=self.org, profile=SimpleNamespace(user=self.user))
        with patch.object(views, "_content_factory_remote_config", return_value=self.remote), \
                patch.object(views, "_content_factory_headers", return_value={}), \
                patch.object(views, "_lookup_content_factory_dispatch_by_key", return_value=("unknown", {})), \
                patch.object(views.http_client, "post", return_value=reply({})):
            run = views._queue_content_factory_run(endpoint="discovery", workflow="auto_discovery",
                context=context, config=SimpleNamespace(github_repo=""), payload=payload)
        self.assertEqual(run.run_id, original["client_request_id"])
        self.assertEqual(run.status, "blocked")
        self.assertTrue(views._run_pending_remote_dispatch(run))
        self.assertNotIn("dispatch_pending_resolution", run.run_request)
        self.assertTrue(all(run.run_request[key] == value for key, value in original.items()))

    def test_changed_actor_refuses_before_http(self):
        post = Mock()
        with self.assertRaises(ArticleGenerationError):
            admitted = reserve_user_dispatch(payload=self.payload(), workflow="auto_discovery", domain=self.org.domain,
                actor_id="foreign-actor", user=self.user)
            post_dispatch(admitted, post, "https://worker.invalid")
        post.assert_not_called()
        self.assertFalse(ContentFactoryRun.objects.exists())

    @override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False)
    def test_flag_off_keeps_exact_legacy_http_and_no_run(self):
        payload = self.payload()
        before = deepcopy(payload)
        response = reply({"job_id": "legacy-job"})
        with patch("integrations.services.article_generation.http_requests.post", return_value=response) as post:
            returned = _post_content_factory_queue_request("https://worker.invalid/api/runs/discovery",
                payload=payload, headers={"X-Test": "same"}, operation="queue_discovery", domain=self.org.domain)
        self.assertIs(returned, response)
        self.assertEqual(payload, before)
        self.assertNotIn(WIRE_REQUEST_KEY, post.call_args.kwargs["json"])
        self.assertEqual(ContentFactoryRun.objects.count(), 0)


class BackendPrivateOwnershipTests(TestCase):
    def test_snapshot_cannot_create_replace_or_erase_admission_or_execution_tickets(self):
        from .service_views import _merge_django_owned_run_request, _merge_django_owned_run_result
        from .run_ownership import R3_BACKEND_REQUEST_KEYS, R3_BACKEND_RESULT_KEYS
        for keys, merge in ((R3_BACKEND_REQUEST_KEYS, _merge_django_owned_run_request),
                            (R3_BACKEND_RESULT_KEYS, _merge_django_owned_run_result)):
            for key in keys:
                original = {key: {"backend": True}}
                self.assertEqual(merge(original, {key: {"worker": True}})[key], original[key])
                self.assertEqual(merge(original, {})[key], original[key])
                self.assertNotIn(key, merge({}, {key: {"worker": True}}))

    def test_public_serializer_strips_private_ledger_and_echo_but_internal_request_retains_echo(self):
        from .service_views import _serialize_content_factory_run
        from .run_ownership import EVENT_RESULT_KEY, WIRE_EXECUTION_KEY
        run = ContentFactoryRun.objects.create(run_id="private-wire-test", workflow="auto_discovery",
            result={EVENT_RESULT_KEY: {"private": "ledger"}, "nested": {OWNER_RESULT_KEY: {"private": True}}, "safe": True},
            run_request={WIRE_REQUEST_KEY: {"private": "identity"}, WIRE_EXECUTION_KEY: {"private": "ticket"}, "topic": "visible"})
        public = _serialize_content_factory_run(run)
        internal = _serialize_content_factory_run(run, include_admission=True)
        self.assertNotIn(EVENT_RESULT_KEY, public["result"])
        self.assertEqual(public["result"]["nested"], {})
        self.assertEqual(public["run_request"], {"topic": "visible"})
        self.assertTrue(public["result"]["safe"])
        self.assertIn(WIRE_REQUEST_KEY, internal["run_request"])
        self.assertNotIn(EVENT_RESULT_KEY, internal["result"])
