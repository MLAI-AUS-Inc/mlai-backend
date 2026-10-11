"""Database-free tests: policy, fencing, exact approvals and opaque cursors.

Run only through scripts/test_without_database.py until the specific schema
and disposable database migration closure have been approved.
"""

from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from agent_runtime import actions, events, policy, runtime


NOW = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)


def grant_fixture():
    owner = SimpleNamespace(pk=123, is_active=True)
    installation = SimpleNamespace(pk=uuid4(), name="Hermes", owner=owner, owner_id=owner.pk, is_active=True)
    return SimpleNamespace(pk=uuid4(), installation=installation, installation_id=installation.pk,
                           scopes=list(policy.SCOPES), resources=["slack:T123:C123"],
                           generation=2, expires_at=NOW + timedelta(days=1), revoked_at=None)


def task_fixture():
    grant = grant_fixture()
    return SimpleNamespace(pk=uuid4(), grant=grant, generation=grant.generation, status="running",
                           resource="", lease_token=uuid4(), lease_epoch=3,
                           lease_expires_at=NOW + timedelta(seconds=60),
                           deadline_at=NOW + timedelta(seconds=300),
                           actual_cost_microunits=0, result={}, save=MagicMock())


@override_settings(AGENT_RUNTIME_ENABLED=True)
class GrantPolicyTests(SimpleTestCase):
    def test_artifact_display_names_preserve_safe_nested_outputs(self):
        for name in ("reports/summary.md", "outputs/結果.csv", "reports/result chart.csv"):
            self.assertEqual(policy.artifact_name(name), name)
        for name in ("/absolute.txt", "../secret", "nested/../secret", "nested//a", "a\\b", "C:/file", "a\x00b", "a\nb", "a" * 161):
            with self.subTest(name=name), self.assertRaises(policy.AgentPermissionDenied):
                policy.artifact_name(name)

    def test_all_three_permission_layers_required(self):
        grant = grant_fixture()
        check = MagicMock(return_value=True)
        policy.authorize(grant, "messages:read", grant.resources[0], access_checker=check, now=NOW)
        check.assert_called_once_with(grant.installation.owner, "messages:read", grant.resources[0])
        for mutation, code in (
            (lambda: setattr(grant, "scopes", ["tasks:read"]), "scope_denied"),
            (lambda: setattr(grant, "resources", []), "resource_denied"),
        ):
            grant = grant_fixture()
            mutation()
            with self.assertRaisesRegex(policy.AgentPermissionDenied, code):
                policy.authorize(grant, "messages:read", "slack:T123:C123", access_checker=check, now=NOW)

    def test_live_acl_unavailable_or_false_always_denies(self):
        grant = grant_fixture()
        for checker in (None, lambda *args: False, lambda *args: "yes", MagicMock(side_effect=TimeoutError)):
            with self.subTest(checker=checker), self.assertRaises(policy.AgentPermissionDenied):
                policy.authorize(grant, "messages:read", grant.resources[0], access_checker=checker, now=NOW)

    def test_revocation_generation_expiry_and_owner_disable(self):
        for field, value in (("revoked_at", NOW), ("expires_at", NOW), ("generation", 3)):
            grant = grant_fixture()
            setattr(grant, field, value)
            with self.subTest(field=field), self.assertRaises(policy.AgentPermissionDenied):
                policy.validate_grant_base(grant, expected_generation=2, now=NOW)
        grant = grant_fixture()
        grant.installation.owner.is_active = False
        with self.assertRaisesRegex(policy.AgentPermissionDenied, "installation_inactive"):
            policy.validate_grant_base(grant, now=NOW)

    @override_settings(AGENT_RUNTIME_ENABLED=False)
    def test_feature_disabled_before_grant_access(self):
        with self.assertRaisesRegex(policy.AgentPermissionDenied, "agent_runtime_disabled"):
            policy.validate_grant_base(None, now=NOW)

    def test_wildcard_unknown_scope_and_noncanonical_resource_rejected(self):
        for resources in (["*"], ["slack:T123:*"], ["https://example.com"], ["slack:../secret"]):
            with self.subTest(resources=resources), self.assertRaises(policy.AgentPermissionDenied):
                policy.validate_resources(resources)
        with self.assertRaises(policy.AgentPermissionDenied):
            policy.validate_scopes(["admin:*"])

    def test_limits_reject_boolean_negative_unknown_and_oversized_values(self):
        for values in ({"max_seconds": True}, {"max_seconds": -1}, {"max_tokens": 2}, {"max_model_calls": 101}):
            with self.subTest(values=values), self.assertRaises(policy.AgentPermissionDenied):
                policy.normalize_limits(values)
        with self.assertRaisesRegex(policy.AgentPermissionDenied, "task_duration_exceeds_grant"):
            policy.normalize_limits({"max_seconds": 500}, max_seconds=300)

    def test_hash_pins_nested_payload_and_rejects_nonfinite_json(self):
        self.assertEqual(policy.canonical_hash({"b": 2, "a": 1}), policy.canonical_hash({"a": 1, "b": 2}))
        self.assertNotEqual(policy.canonical_hash({"a": {"owner": 1}}), policy.canonical_hash({"a": {"owner": 2}}))
        with self.assertRaises(policy.AgentPermissionDenied):
            policy.bounded_json({"cost": float("nan")})


class QuietHoursTests(SimpleTestCase):
    def test_overnight_quiet_hours_defer_in_melbourne(self):
        instant = datetime(2026, 10, 5, 12, 15, tzinfo=timezone.utc)  # 23:15 AEDT
        wake = policy.after_quiet_hours(instant, timezone_name="Australia/Melbourne", quiet_start=time(22), quiet_end=time(8))
        self.assertEqual(wake, datetime(2026, 10, 5, 21, 0, tzinfo=timezone.utc))

    def test_spring_forward_nonexistent_end_time_is_not_invented(self):
        instant = datetime(2026, 10, 3, 15, 30, tzinfo=timezone.utc)  # 01:30 AEST
        wake = policy.after_quiet_hours(instant, timezone_name="Australia/Melbourne", quiet_start=time(1), quiet_end=time(2, 30))
        self.assertEqual(wake, datetime(2026, 10, 3, 16, 0, tzinfo=timezone.utc))  # 03:00 AEDT

    def test_outside_quiet_window_does_not_move_wake(self):
        self.assertEqual(policy.after_quiet_hours(NOW, timezone_name="Australia/Melbourne", quiet_start=time(22), quiet_end=time(8)), NOW)

    def test_missing_partner_invalid_zone_and_naive_time_rejected(self):
        for kwargs in ({"timezone_name": "Australia/Melbourne", "quiet_start": time(22)}, {"timezone_name": "unknown"}):
            with self.assertRaises(policy.AgentPermissionDenied):
                policy.after_quiet_hours(NOW, **kwargs)
        with self.assertRaises(policy.AgentPermissionDenied):
            policy.after_quiet_hours(NOW.replace(tzinfo=None), timezone_name="UTC")


@override_settings(AGENT_RUNTIME_ENABLED=True)
class LeaseTests(SimpleTestCase):
    def test_current_lease_passes(self):
        task = task_fixture()
        runtime.require_lease(task, str(task.lease_token), task.lease_epoch, now=NOW)

    def test_replaced_worker_expired_lease_and_deadline_all_reject(self):
        for key, value in (("lease_token", uuid4()), ("lease_epoch", 4), ("lease_expires_at", NOW), ("deadline_at", NOW), ("status", "cancelled")):
            task = task_fixture()
            original_token, original_epoch = task.lease_token, task.lease_epoch
            setattr(task, key, value)
            with self.subTest(key=key), self.assertRaisesRegex(runtime.AgentRuntimeError, "lease_lost"):
                runtime.require_lease(task, original_token, original_epoch, now=NOW)

    def test_completed_result_never_saved_by_stale_worker(self):
        task = task_fixture()
        with patch.object(runtime, "_task_with_grant_lock", return_value=task):
            with self.assertRaisesRegex(runtime.AgentRuntimeError, "lease_lost"):
                runtime.complete_task.__wrapped__(task.pk, lease_token=uuid4(), lease_epoch=task.lease_epoch, result={"text": "stale"}, now=NOW)
        task.save.assert_not_called()
        self.assertEqual(task.result, {})

    def test_grant_generation_change_fences_valid_worker_token(self):
        task = task_fixture()
        task.grant.generation += 1
        with self.assertRaisesRegex(policy.AgentPermissionDenied, "grant_changed"):
            runtime.require_lease(task, task.lease_token, task.lease_epoch, now=NOW)

    def test_completed_callback_retries_match_exact_receipt_only(self):
        task = task_fixture()
        token, epoch = task.lease_token, task.lease_epoch
        with (patch.object(runtime, "_task_with_grant_lock", return_value=task),
              patch.object(runtime, "AgentCredential"), patch.object(runtime, "audit")):
            first = runtime.complete_task.__wrapped__(task.pk, lease_token=token, lease_epoch=epoch, result={"text": "done"}, now=NOW)
            self.assertEqual(first.status, "completed")
            self.assertIsNone(first.lease_token)
            self.assertNotIn(str(token), str(first.result))
            task.save.reset_mock()
            self.assertIs(runtime.complete_task.__wrapped__(task.pk, lease_token=token, lease_epoch=epoch, result={"text": "done"}, now=NOW), task)
            task.save.assert_not_called()
            for changes in ({"lease_token": uuid4()}, {"result": {"text": "changed"}}, {"actual_cost_microunits": 2}):
                kwargs = {"lease_token": token, "lease_epoch": epoch, "result": {"text": "done"}, "now": NOW, **changes}
                with self.subTest(changes=changes), self.assertRaisesRegex(runtime.AgentRuntimeError, "completion_conflict"):
                    runtime.complete_task.__wrapped__(task.pk, **kwargs)

    def test_caller_cannot_supply_completion_receipt(self):
        task = task_fixture()
        with patch.object(runtime, "_task_with_grant_lock", return_value=task):
            with self.assertRaisesRegex(runtime.AgentRuntimeError, "invalid_result"):
                runtime.complete_task.__wrapped__(task.pk, lease_token=task.lease_token, lease_epoch=task.lease_epoch,
                                                  result={"_completion": {"fake": True}}, now=NOW)

    def test_worker_completion_cannot_lower_trusted_or_ambiguous_model_cost(self):
        task = task_fixture()
        task.actual_cost_microunits = 900
        with (patch.object(runtime, "_task_with_grant_lock", return_value=task),
              patch.object(runtime, "AgentCredential"), patch.object(runtime, "audit")):
            runtime.complete_task.__wrapped__(task.pk, lease_token=task.lease_token, lease_epoch=task.lease_epoch,
                                              result={"text": "done"}, actual_cost_microunits=1, now=NOW)
        self.assertEqual(task.actual_cost_microunits, 900)

    def test_command_denial_details_are_bounded_data_and_never_retried(self):
        task = task_fixture()
        task.attempts, task.max_attempts = 1, 3
        details = {"command": "rm -rf /workspace/draft", "reason": "Command requires review"}
        with (patch.object(runtime, "_task_with_grant_lock", return_value=task),
              patch.object(runtime, "AgentCredential"), patch.object(runtime, "audit")):
            runtime.fail_task.__wrapped__(task.pk, lease_token=task.lease_token, lease_epoch=task.lease_epoch,
                                          error_code="approval_required", retryable=True, details=details, now=NOW)
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.error_code, "approval_required")
        self.assertEqual(task.result, {"review_required": details})
        self.assertIsNone(task.lease_token)

    @override_settings(AGENT_RUNTIME_ENABLED=False)
    def test_disabled_scheduler_and_claim_make_no_database_calls(self):
        self.assertIsNone(runtime.claim_task(worker_id="worker-1"))
        self.assertEqual(runtime.recover_tasks(), 0)
        self.assertEqual(runtime.schedule_due_responsibilities(), 0)
        self.assertEqual(events.dispatch_subscriptions(), 0)
        self.assertEqual(actions.recover_actions(), 0)


@override_settings(AGENT_RUNTIME_ENABLED=True, AGENT_RUNTIME_ACTIONS_ENABLED=True)
class ActionTests(SimpleTestCase):
    def action(self):
        task = task_fixture()
        task.resource = "slack:T123:C123"
        action = SimpleNamespace(pk=uuid4(), task=task, generation=task.generation,
                                 kind="message.send", resource="slack:T123:C123",
                                 payload={"text": "Reviewed text"}, expires_at=NOW + timedelta(hours=1),
                                 status="sending", lease_token=uuid4(), lease_expires_at=NOW + timedelta(seconds=60),
                                 save=MagicMock())
        action.payload_hash = actions.action_hash(task_id=task.pk, generation=action.generation,
                                                 kind=action.kind, resource=action.resource, payload=action.payload)
        action.approval_hash = action.payload_hash
        return action

    def test_approval_pins_destination_and_body(self):
        for field, value in (("resource", "slack:T123:C999"), ("payload", {"text": "Altered"})):
            action = self.action()
            action.task.grant.resources.append("slack:T123:C999")
            setattr(action, field, value)
            expected = "task_resource_mismatch" if field == "resource" else "action_changed"
            with self.subTest(field=field), self.assertRaisesRegex(policy.AgentPermissionDenied, expected):
                actions._check_action(action, access_checker=lambda *args: True, now=NOW, require_approved=True)

    def test_timeout_or_malformed_receipt_is_unknown_never_requeued(self):
        for executor in (MagicMock(side_effect=TimeoutError), lambda *args, **kwargs: {"ok": True}):
            action = self.action()
            with patch.object(actions, "_get_action", return_value=action), patch.object(actions, "audit"):
                result = actions.deliver_claimed_action.__wrapped__(action.pk, lease_token=action.lease_token,
                                                                    executor=executor, access_checker=lambda *args: True, now=NOW)
            self.assertEqual(result.status, "unknown")
            self.assertIsNone(result.lease_token)

    def test_revocation_after_claim_prevents_external_call(self):
        action = self.action()
        action.task.grant.revoked_at = NOW
        executor = MagicMock()
        with patch.object(actions, "_get_action", return_value=action):
            result = actions.deliver_claimed_action.__wrapped__(action.pk, lease_token=action.lease_token,
                                                                executor=executor, access_checker=lambda *args: True, now=NOW)
        executor.assert_not_called()
        self.assertEqual(result.status, "cancelled")

    def test_no_raw_url_or_actor_override_in_message_payload(self):
        for payload in ({"text": "hi", "url": "https://evil.example"}, {"text": "hi", "owner_id": 5},
                        {"text": "hi", "thread_id": "../secret"}, {"text": "hi", "thread_id": "not-a-slack-ts"},
                        {"text": "   "}, {"text": "<!channel> hi"}):
            with self.assertRaises(policy.AgentPermissionDenied):
                actions.validate_action_payload("message.send", payload)

    def test_attribution_is_present_before_payload_is_hashed(self):
        task = task_fixture()
        payload = actions.attributed_payload(task, {"text": "The report is ready"})
        self.assertEqual(payload["text"], "[Agent Hermes, acting for member]\n\nThe report is ready")
        args = dict(task_id=task.pk, generation=task.generation, kind="message.send", resource="slack:T123:C123")
        self.assertNotEqual(actions.action_hash(**args, payload=payload), actions.action_hash(**args, payload={"text": "The report is ready"}))

    @override_settings(AGENT_RUNTIME_ACTIONS_ENABLED=False)
    def test_action_kill_switch_denies_already_approved_payload(self):
        with self.assertRaisesRegex(policy.AgentPermissionDenied, "agent_actions_disabled"):
            actions._check_action(self.action(), access_checker=lambda *args: True, now=NOW, require_approved=True)


@override_settings(SECRET_KEY="synthetic-event-cursor-key")
class EventCursorTests(SimpleTestCase):
    def test_cursor_is_opaque_and_bound_to_grant_generation_and_resource(self):
        grant = grant_fixture()
        cursor = events.encode_cursor(grant, "slack:T123:C123", 91234567)
        self.assertNotIn("91234567", cursor)
        self.assertEqual(events.decode_cursor(grant, "slack:T123:C123", cursor), 91234567)
        with self.assertRaises(runtime.AgentRuntimeError):
            events.decode_cursor(grant, "slack:T123:C999", cursor)
        grant.generation += 1
        with self.assertRaises(runtime.AgentRuntimeError):
            events.decode_cursor(grant, "slack:T123:C123", cursor)

    def test_other_grant_tamper_numeric_and_oversized_cursor_rejected(self):
        grant = grant_fixture()
        cursor = events.encode_cursor(grant, "slack:T123:C123", 1)
        for bad in (cursor[:-10] + "malformed", 1, "a" * 2049):
            with self.subTest(bad=str(bad)[:20]), self.assertRaises(runtime.AgentRuntimeError):
                events.decode_cursor(grant, "slack:T123:C123", bad)
        with self.assertRaises(runtime.AgentRuntimeError):
            events.decode_cursor(grant_fixture(), "slack:T123:C123", cursor)

    def test_agent_messages_and_chain_depth_do_not_loop_by_default(self):
        subscription = SimpleNamespace(is_active=True, resource="slack:T123:C123", event_types=["message.created"], allow_agent_events=False, max_chain_depth=1)
        event = SimpleNamespace(resource=subscription.resource, event_type="message.created", actor_kind="agent", chain_depth=0)
        self.assertFalse(events.should_wake(subscription, event))
        event.actor_kind = "human"
        self.assertTrue(events.should_wake(subscription, event))
        event.chain_depth = 1
        self.assertFalse(events.should_wake(subscription, event))

    def test_resource_lock_requires_transaction_and_fixed_database_operation(self):
        with patch.object(events, "connection") as connection:
            connection.in_atomic_block = False
            with self.assertRaisesRegex(runtime.AgentRuntimeError, "event_lock_requires_transaction"):
                events.lock_event_resource("slack:T123:C123")
            connection.cursor.assert_not_called()
            connection.in_atomic_block, connection.vendor = True, "postgresql"
            cursor = connection.cursor.return_value.__enter__.return_value
            events.lock_event_resource("slack:T123:C123")
            events.lock_event_resource("slack:T123:C123")
            self.assertEqual(cursor.execute.call_args_list[0], cursor.execute.call_args_list[1])
            sql, params = cursor.execute.call_args.args
            self.assertEqual(sql, "SELECT pg_advisory_xact_lock(%s, %s)")
            self.assertTrue(all(type(value) is int for value in params))
            connection.vendor = "mysql"
            with self.assertRaisesRegex(runtime.AgentRuntimeError, "unsupported_event_database"):
                events.lock_event_resource("slack:T123:C123")


@override_settings(AGENT_RUNTIME_ENABLED=True)
class ScopedMemoryTests(SimpleTestCase):
    def test_prior_outcomes_filtered_by_all_authority_dimensions(self):
        grant = grant_fixture()
        responsibility = SimpleNamespace(pk=uuid4(), grant_id=grant.pk, generation=grant.generation, resource=grant.resources[0])
        previous = SimpleNamespace(pk=uuid4(), result={"text": "a" * 9000, "secret_internal": "not copied"}, finished_at=NOW)
        with patch.object(runtime, "AgentTask") as model:
            model.objects.filter.return_value.order_by.return_value.__getitem__.return_value = [previous]
            memories = runtime.scoped_memory(grant, responsibility=responsibility, resource=responsibility.resource,
                                             access_checker=lambda *args: True, now=NOW)
            model.objects.filter.assert_called_once_with(grant=grant, generation=grant.generation, responsibility=responsibility,
                                                          resource=responsibility.resource, status="completed")
        self.assertEqual(len(memories[0]["prior_task_result"]), 2000)
        self.assertTrue(memories[0]["untrusted_source"])
        self.assertNotIn("not copied", str(memories))

    def test_changed_generation_other_grant_resource_or_lost_acl_never_reads_history(self):
        grant = grant_fixture()
        for mutation in ({"grant_id": uuid4()}, {"resource": "slack:T123:C999"}, {"generation": 99}):
            responsibility = SimpleNamespace(grant_id=grant.pk, resource=grant.resources[0], generation=grant.generation)
            for key, value in mutation.items():
                setattr(responsibility, key, value)
            with patch.object(runtime, "AgentTask") as model, self.assertRaises(policy.AgentPermissionDenied):
                runtime.scoped_memory(grant, responsibility=responsibility, resource=grant.resources[0], access_checker=lambda *args: True, now=NOW)
            model.objects.filter.assert_not_called()

        responsibility = SimpleNamespace(grant_id=grant.pk, resource=grant.resources[0], generation=grant.generation)
        with patch.object(runtime, "AgentTask") as model, self.assertRaises(policy.AgentPermissionDenied):
            runtime.scoped_memory(grant, responsibility=responsibility, resource=grant.resources[0],
                                  access_checker=lambda *args: False, now=NOW)
        model.objects.filter.assert_not_called()


class SchedulerOptInTests(SimpleTestCase):
    @override_settings(AGENT_RUNTIME_ENABLED=False)
    def test_disabled_tick_stops_before_queries(self):
        from django.core.management.base import CommandError
        from agent_runtime.management.commands.run_agent_scheduler import tick
        with patch.object(runtime, "recover_tasks") as recover, self.assertRaises(CommandError):
            tick(schedule=True, fanout=True, deliver=True, webhooks=True)
        recover.assert_not_called()

    @override_settings(AGENT_RUNTIME_ENABLED=True, AGENT_RUNTIME_ACTIONS_ENABLED=False)
    def test_default_tick_only_recovers_and_action_flag_is_separate(self):
        from django.core.management.base import CommandError
        from agent_runtime.management.commands.run_agent_scheduler import tick
        with (patch.object(runtime, "recover_tasks", return_value=2) as recover_tasks,
              patch.object(actions, "recover_actions", return_value=1),
              patch.object(runtime, "schedule_due_responsibilities") as schedule,
              patch.object(events, "dispatch_subscriptions") as fanout,
              patch.object(actions, "claim_action") as claim):
            result = tick()
            self.assertEqual((result["recovered_tasks"], result["unknown_actions"]), (2, 1))
            schedule.assert_not_called()
            fanout.assert_not_called()
            claim.assert_not_called()
            recover_tasks.reset_mock()
            with self.assertRaises(CommandError):
                tick(deliver=True)
            recover_tasks.assert_not_called()


class RegistrationTests(SimpleTestCase):
    def test_app_is_migration_managed_and_cannot_enter_syncdb_set(self):
        from django.db.migrations.loader import MigrationLoader
        loader = MigrationLoader(None)
        self.assertIn("agent_runtime", loader.migrated_apps)
        self.assertNotIn("agent_runtime", loader.unmigrated_apps)

    @override_settings(AGENT_RUNTIME_ENABLED=False)
    def test_disabled_admin_does_not_offer_unmigrated_tables(self):
        from django.contrib.admin import site
        from agent_runtime.admin import ReadOnlyAgentAdmin
        from agent_runtime.models import AgentGrant
        view = ReadOnlyAgentAdmin(AgentGrant, site)
        request = SimpleNamespace(user=MagicMock())
        self.assertFalse(view.has_module_permission(request))
        self.assertFalse(view.has_view_permission(request))
        request.user.has_perm.assert_not_called()

    @override_settings(AGENT_RUNTIME_ENABLED=False)
    def test_every_registered_runtime_route_stops_before_database_access(self):
        from rest_framework.test import APIRequestFactory
        from agent_runtime.urls import urlpatterns
        from agent_runtime.views import OAuthMetadataView, ResourceMetadataView
        factory = APIRequestFactory()
        for route in urlpatterns:
            callback = route.callback
            view_class = getattr(callback, "view_class", None) or callback.cls
            method = "get" if hasattr(view_class, "get") else "post"
            request = getattr(factory, method)("/api/v1/agent-runtime/test/", {}, format="json")
            kwargs = {name: uuid4() for name in route.pattern.converters}
            with self.subTest(route=str(route.pattern), method=method):
                self.assertEqual(callback(request, **kwargs).status_code, 404)
        for view in (OAuthMetadataView, ResourceMetadataView):
            self.assertEqual(view.as_view()(factory.get("/.well-known/test")).status_code, 404)
