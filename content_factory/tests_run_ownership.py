"""Actual database admission/binding tests; no provider calls are made."""
from django.db import connections
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

from django.db import close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from unittest import skipUnless

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep, ContentFactoryRunStepAttempt

from .run_ownership import (
    EVENT_RESULT_KEY, OWNER_EXECUTION_KEY, OWNER_RESULT_KEY, WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY,
    RunOwnershipConflict, admitted_run_for_key, authorized_event_generation, bind_admitted_run,
    current_execution_ticket, owner_receipt, prepare_owned_callback, require_event_execution,
    is_hosted_proxy_request, require_event_identity, reserve_admitted_run, scheduler_principal,
    seal_new_run_admission, user_principal, worker_workflow_allowed,
)

FLAGS = {"ARTICLES_RUN_OWNERSHIP_ENABLED": True, "ARTICLES_RUN_FACT_AUTHORITY_ENABLED": False,
         "ARTICLES_RUN_EVENT_AUTHORITY_ENABLED": False}


@override_settings(**FLAGS)
class RunAdmissionTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic R3", domain="r3.example")
        self.principal = user_principal(123, "user:123")

    def reserve(self, payload=None, **kwargs):
        return reserve_admitted_run(organization=self.org, principal=self.principal,
            workflow="auto_discovery", payload=payload if payload is not None else self.payload(), **kwargs)

    def payload(self, **kwargs):
        return {"domain": self.org.domain, "client_request_id": "admitted-key", "topic": "Original", **kwargs}

    def test_flag_off_does_not_query_or_mutate_payload(self):
        payload = self.payload()
        before = deepcopy(payload)
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False), self.assertNumQueries(0):
            self.assertIsNone(self.reserve(payload))
        self.assertEqual(payload, before)

    def test_reservation_exists_before_worker_identity_or_job_facts(self):
        payload = self.payload(github_token="never-persist")
        run = self.reserve(payload)
        self.assertEqual(run.run_id, "admitted-key")
        self.assertEqual(run.organization_id, self.org.pk)
        self.assertEqual(owner_receipt(run)["identity"]["principal"], self.principal)
        self.assertNotIn("github_token", run.run_request)
        self.assertIn(WIRE_REQUEST_KEY, payload)
        self.assertEqual(owner_receipt(run)["authority"], {"backend_facts": False, "event_protocol": None})
        self.assertEqual(authorized_event_generation(run), 0)
        self.assertIsNone(run.job_created_at)
        self.assertEqual(run.delivery_phase, "")

    def test_replay_retains_first_authority_and_one_canonical_row(self):
        payload = self.payload()
        first = self.reserve(payload)
        with override_settings(ARTICLES_RUN_FACT_AUTHORITY_ENABLED=True, ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=True):
            replay = self.reserve(payload)
        self.assertEqual(replay.pk, first.pk)
        self.assertEqual(owner_receipt(replay)["authority"], {"backend_facts": False, "event_protocol": None})
        self.assertEqual(ContentFactoryRun.objects.count(), 1)

    def test_admission_off_keeps_existing_receipt_but_cannot_create_from_wire(self):
        payload = self.payload()
        first = self.reserve(payload)
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False):
            self.assertEqual(self.reserve(payload).pk, first.pk)
            bad = deepcopy(payload)
            bad["client_request_id"] = "different"
            with self.assertRaises(RunOwnershipConflict):
                self.reserve(bad)

    def test_same_key_changed_body_actor_source_or_workflow_refuses(self):
        self.reserve()
        for change in ({"topic": "Other"}, {"source_run_id": "foreign"}, {"roo_points_cost": 999}):
            with self.subTest(change=change), self.assertRaises(RunOwnershipConflict):
                self.reserve(self.payload(**change))
        with self.assertRaises(RunOwnershipConflict):
            reserve_admitted_run(organization=self.org, principal=user_principal(124, "user:124"),
                workflow="auto_discovery", payload=self.payload())
        with self.assertRaises(RunOwnershipConflict):
            reserve_admitted_run(organization=self.org, principal=self.principal,
                workflow="article_generation", payload=self.payload())

    def test_foreign_org_same_key_cannot_adopt(self):
        self.reserve()
        other = Organization.objects.create(name="Other", domain="other-r3.example")
        with self.assertRaises(RunOwnershipConflict):
            reserve_admitted_run(organization=other, principal=self.principal, workflow="auto_discovery",
                payload=self.payload(domain=other.domain))

    def test_legacy_record_not_enrolled_or_mutated(self):
        old = ContentFactoryRun.objects.create(run_id="admitted-key", domain=self.org.domain,
            workflow="auto_discovery", organization=self.org, result={"history": "retained"})
        before = ContentFactoryRun.objects.values().get(pk=old.pk)
        with self.assertRaises(RunOwnershipConflict):
            self.reserve()
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=old.pk), before)

    def test_request_key_and_domain_are_not_normalized_into_admission(self):
        for payload in (self.payload(client_request_id=" key "), self.payload(client_request_id=True),
                        self.payload(client_request_id="x" * 256), self.payload(domain="foreign.example")):
            with self.subTest(payload=payload), self.assertRaises(RunOwnershipConflict):
                self.reserve(payload)
        self.assertEqual(ContentFactoryRun.objects.count(), 0)

    def test_long_original_key_retained_without_truncation_or_new_schema(self):
        payload = self.payload(client_request_id="original-key:" + "x" * 150)
        run = self.reserve(payload)
        self.assertLessEqual(len(run.run_id), 100)
        self.assertEqual(owner_receipt(run)["identity"]["dispatch_key"], payload["client_request_id"])
        self.assertEqual(admitted_run_for_key(payload["client_request_id"]).pk, run.pk)
        self.assertEqual(bind_admitted_run(run, "real-long-key-worker").pk, run.pk)

    def test_preselected_backend_worker_id_is_fixed_before_remote_dispatch(self):
        payload = self.payload(run_id="preselected-backend-child")
        run = self.reserve(payload, reserved_run_id="preselected-backend-child")
        self.assertEqual(run.run_id, "preselected-backend-child")
        self.assertEqual(bind_admitted_run(run, run.run_id).pk, run.pk)
        with self.assertRaises(RunOwnershipConflict):
            bind_admitted_run(run, "different-child")

    def test_seal_new_named_run_retains_charge_approval_and_exact_clocks(self):
        from .website_contract import evidence_digest
        from roo.models import Ledger
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user(email="r3-seal@example.test")
        ledger = Ledger.objects.create(user=user, kind="SPEND", source="CONTENT_FACTORY",
            delta=-6, delta_microroo=-6_000_000, idempotency_key="synthetic-original-r3-charge")
        charge_before = Ledger.objects.values().get(pk=ledger.pk)
        request = self.payload(client_request_id="named-publish", roo_points_ledger_id=ledger.pk,
            original_billing_source_run_id="original-draft", approved_body_sha256="b" * 64)
        retained = {"article_approval_v1": {"approved_body_sha256": "b" * 64},
                    "points_v1": {"state": "carried", "original_key": ledger.idempotency_key}}
        with transaction.atomic():
            run = ContentFactoryRun.objects.create(run_id="named-publish", organization=self.org,
                workflow="auto_discovery", domain=self.org.domain, run_request=request, result=retained)
            before = ContentFactoryRun.objects.values().get(pk=run.pk)
            seal_new_run_admission(run, created=True, principal=self.principal)
        run.refresh_from_db()
        self.assertEqual(owner_receipt(run)["identity"]["request_digest"], evidence_digest(request))
        for key, value in retained.items():
            self.assertEqual(run.result[key], value)
        self.assertEqual(run.created_at, before["created_at"])
        self.assertEqual(run.updated_at, before["updated_at"])
        self.assertEqual(Ledger.objects.values().get(pk=ledger.pk), charge_before)
        self.assertEqual(Ledger.objects.count(), 1)

    def test_seal_never_enrolls_existing_or_previously_active_execution(self):
        for field, value in ((None, None), ("progress_message_ts", "123.4"), ("current_step", "writing"),
                             ("last_event_emitted_at", timezone.now())):
            with self.subTest(field=field), transaction.atomic():
                request = self.payload(client_request_id="seal-" + str(field))
                kwargs = {field: value} if field else {}
                run = ContentFactoryRun.objects.create(run_id=request["client_request_id"], organization=self.org,
                    workflow="auto_discovery", domain=self.org.domain, run_request=request, **kwargs)
                before = ContentFactoryRun.objects.values().get(pk=run.pk)
                with self.assertRaises(RunOwnershipConflict):
                    seal_new_run_admission(run, created=bool(field), principal=self.principal)
                self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)

    def test_seal_rolls_back_with_new_creation_before_any_dispatch(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                run = ContentFactoryRun.objects.create(run_id="rollback-new", organization=self.org,
                    workflow="auto_discovery", domain=self.org.domain, run_request=self.payload(client_request_id="rollback-new"))
                seal_new_run_admission(run, created=True, principal=self.principal)
                raise RuntimeError("synthetic crash before dispatch")
        self.assertFalse(ContentFactoryRun.objects.filter(run_id="rollback-new").exists())

    def test_scheduler_has_explicit_service_not_substituted_owner(self):
        run = reserve_admitted_run(organization=self.org, principal=scheduler_principal("island_refresh"),
            workflow="island_refresh", payload=self.payload())
        self.assertEqual(owner_receipt(run)["identity"]["principal"], {"kind": "scheduler", "service": "island_refresh"})
        self.assertEqual(run.slack_user_id, "")
        for value in (None, True, 0, "foreign"):
            with self.subTest(value=value), self.assertRaises(RunOwnershipConflict):
                user_principal(value, "actor")

    def test_hosted_proxy_alias_requires_exact_saved_server_recipe_context(self):
        from uuid import UUID
        site = UUID("12121212-1212-4212-8212-121212121212")
        proxy = {"hosted_site_id": str(site), "worker_url": "https://r3-owned.example.workers.dev",
            "run_id": f"hosted-proxy-{site.hex}-3", "connection_generation": 3, "merge_policy": "backend_worker"}
        self.assertTrue(is_hosted_proxy_request(proxy))
        self.assertTrue(worker_workflow_allowed("article_system_setup", "hosted_native_verification", request=proxy))
        self.assertFalse(worker_workflow_allowed("article_system_setup", "hosted_native_verification"))
        for change in ({"hosted_site_id": None}, {"worker_url": "https://customer.example"},
                       {"worker_url": "https://r3-owned.example.workers.dev:bad"},
                       {"worker_url": "https://user:password@r3-owned.example.workers.dev"},
                       {"connection_generation": True}, {"run_id": "ordinary-setup"}, {"merge_policy": None}):
            with self.subTest(change=change):
                self.assertFalse(worker_workflow_allowed("article_system_setup", "hosted_native_verification", request={**proxy, **change}))

    def test_bind_preserves_pk_steps_attempts_receipts_and_all_clocks(self):
        run = self.reserve()
        result = {**run.result, EVENT_RESULT_KEY: {"history": [{"seq": 1}]}, "points_v1": {"state": "reserved"}}
        ContentFactoryRun.objects.filter(pk=run.pk).update(result=result)
        run.refresh_from_db()
        step = ContentFactoryRunStep.objects.create(run=run, step_key="first")
        attempt = ContentFactoryRunStepAttempt.objects.create(step=step, attempt=1)
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        bound = bind_admitted_run(run, "worker-real")
        after = ContentFactoryRun.objects.values().get(pk=run.pk)
        self.assertEqual(after, {**before, "run_id": "worker-real"})
        self.assertEqual(bound.pk, run.pk)
        self.assertTrue(ContentFactoryRunStepAttempt.objects.filter(pk=attempt.pk, step__run_id=run.pk).exists())
        self.assertEqual(admitted_run_for_key("admitted-key").pk, run.pk)
        self.assertEqual(self.reserve().pk, run.pk)
        self.assertEqual(bind_admitted_run(bound, "worker-real").pk, run.pk)
        with self.assertRaises(RunOwnershipConflict):
            bind_admitted_run(bound, "different-worker")

    def test_real_id_collision_never_deletes_either_record_or_history(self):
        run = self.reserve()
        real = ContentFactoryRun.objects.create(run_id="occupied", workflow="auto_discovery", domain="")
        before = list(ContentFactoryRun.objects.order_by("pk").values())
        with self.assertRaises(RunOwnershipConflict):
            bind_admitted_run(run, real.run_id)
        self.assertEqual(list(ContentFactoryRun.objects.order_by("pk").values()), before)

    def test_binding_renames_only_the_existing_job_carrier_without_new_charge_or_clocks(self):
        from .models import ContentFactoryJob
        run = self.reserve()
        job = ContentFactoryJob.objects.create(job_id=run.run_id, domain=self.org.domain, slack_user_id="U_SYNTH",
            billing_source_job_id="original-charge-source", billing_status="charged", billing_amount=6)
        before = ContentFactoryJob.objects.values().get(pk=job.pk)
        bind_admitted_run(run, "actual-worker-with-carrier")
        self.assertEqual(ContentFactoryJob.objects.values().get(pk=job.pk), {**before, "job_id": "actual-worker-with-carrier"})

    def test_occupied_job_carrier_refuses_before_alias_rename(self):
        from .models import ContentFactoryJob
        run = self.reserve()
        ContentFactoryJob.objects.create(job_id=run.run_id, domain=self.org.domain, slack_user_id="U_SYNTH")
        ContentFactoryJob.objects.create(job_id="legacy-occupied", domain="foreign.example", slack_user_id="U_FOREIGN")
        before = list(ContentFactoryJob.objects.order_by("pk").values())
        with self.assertRaises(RunOwnershipConflict):
            bind_admitted_run(run, "legacy-occupied")
        run.refresh_from_db()
        self.assertEqual(run.run_id, "admitted-key")
        self.assertEqual(list(ContentFactoryJob.objects.order_by("pk").values()), before)

    @override_settings(ARTICLES_RUN_FACT_AUTHORITY_ENABLED=True, ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=True)
    def test_binding_releases_only_private_transport_hold_without_clock_or_receipt_rewrite(self):
        from .dispatch_admission import pending_response
        run = self.reserve()
        pending_response(run)
        run.refresh_from_db()
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        bound = bind_admitted_run(run, "real-from-held-dispatch")
        after = ContentFactoryRun.objects.values().get(pk=run.pk)
        self.assertEqual(after, {**before, "run_id": bound.run_id, "status": "queued", "current_step": "queued"})
        for status, step in (("blocked", "verification"), ("failed", "dispatch_pending_resolution"),
                             ("cancelled", "dispatch_pending_resolution"), ("denied", "dispatch_pending_resolution"),
                             ("completed", "dispatch_pending_resolution")):
            ContentFactoryRun.objects.filter(pk=run.pk).update(status=status, current_step=step)
            with self.subTest(status=status, step=step):
                again = bind_admitted_run(bound, bound.run_id)
                self.assertEqual((again.status, again.current_step), (status, step))

    @override_settings(ARTICLES_RUN_FACT_AUTHORITY_ENABLED=True, ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=True)
    def test_event_echo_requires_complete_exact_scope_and_protocol(self):
        payload = self.payload()
        run = self.reserve(payload)
        event = {WIRE_REQUEST_KEY: payload[WIRE_REQUEST_KEY], "client_request_id": "admitted-key"}
        self.assertEqual(require_event_identity(run, event), owner_receipt(run))
        for mutate in (lambda p: p[WIRE_REQUEST_KEY].pop("scope"),
                       lambda p: p[WIRE_REQUEST_KEY].update(event_protocol=True),
                       lambda p: p[WIRE_REQUEST_KEY]["scope"].update(source_run_id="foreign"),
                       lambda p: p.update(client_request_id="foreign"),
                       lambda p: p.update(worker_workflow="article_generation")):
            bad = deepcopy(event)
            mutate(bad)
            with self.assertRaises(RunOwnershipConflict):
                require_event_identity(run, bad)

    def test_reservation_only_cannot_accept_authoritative_events(self):
        payload = self.payload()
        run = self.reserve(payload)
        with self.assertRaises(RunOwnershipConflict):
            require_event_identity(run, {WIRE_REQUEST_KEY: payload[WIRE_REQUEST_KEY]})
        with override_settings(ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=True), self.assertRaises(RunOwnershipConflict):
            self.reserve(self.payload(client_request_id="new-event-key"))

    def test_callback_validates_typed_version_and_ticket_before_any_binding(self):
        payload = self.payload()
        run = self.reserve(payload)
        valid = {**payload, "generation": 0, "state_version": 1, "workflow": "auto_discovery"}
        for change in ({"generation": True}, {"generation": 1}, {"state_version": None},
                       {WIRE_REQUEST_KEY: {}}, {WIRE_EXECUTION_KEY: {}}, {"workflow": "repo_scan"}):
            with self.subTest(change=change), self.assertRaises(RunOwnershipConflict):
                prepare_owned_callback({**valid, **change}, "callback-worker")
            run.refresh_from_db()
            self.assertEqual(run.run_id, "admitted-key")
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        bound = prepare_owned_callback(valid, "callback-worker")
        self.assertEqual(bound.pk, run.pk)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), {**before, "run_id": "callback-worker"})

    def test_unknown_admission_and_missing_echo_cannot_materialize_callback(self):
        payload = self.payload()
        run = self.reserve(payload)
        with self.assertRaises(RunOwnershipConflict):
            prepare_owned_callback({"client_request_id": "admitted-key", "generation": 0, "state_version": 1}, "new-worker")
        with self.assertRaises(RunOwnershipConflict):
            prepare_owned_callback({**payload, WIRE_REQUEST_KEY: {"dispatch_key": "foreign"},
                "client_request_id": "foreign", "generation": 0, "state_version": 1}, "new-worker")
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        self.assertIsNone(prepare_owned_callback({"generation": 0, "state_version": 1}, "legacy-unknown"))
        run.refresh_from_db()
        self.assertEqual(run.run_id, "admitted-key")

    def test_ticket_scope_and_bool_versions_are_exact_and_cannot_be_self_granted(self):
        payload = self.payload()
        run = self.reserve(payload)
        self.assertEqual(require_event_execution(run, payload, generation=0, current=True), current_execution_ticket(run))
        for change in ({"generation": True}, {"scope": {}}, {"execution_id": "foreign"},
                       {"request_digest": "a" * 64}, {"admission_digest": "a" * 64}):
            bad = deepcopy(payload)
            bad[WIRE_EXECUTION_KEY].update(change)
            with self.subTest(change=change), self.assertRaises(RunOwnershipConflict):
                require_event_execution(run, bad, generation=bad[WIRE_EXECUTION_KEY].get("generation"))


@skipUnless(connection.vendor == "postgresql", "Concurrency requires PostgreSQL row locking.")
@override_settings(**FLAGS)
class ConcurrentRunAdmissionTests(TransactionTestCase):
    def test_same_key_creates_one_canonical_admission(self):
        org = Organization.objects.create(name="Concurrent R3", domain="concurrent-r3.example")
        gate = Barrier(2)

        def reserve():
            close_old_connections()
            try:
                gate.wait(timeout=10)
                run = reserve_admitted_run(organization=org, principal=user_principal(321, "user:321"),
                    workflow="auto_discovery", payload={"domain": org.domain, "client_request_id": "concurrent-key"})
                return run.pk
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: reserve(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
