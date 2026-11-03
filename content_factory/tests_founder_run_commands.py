"""Real bearer/facade/SQL command replay; only external CF transport is fake."""
from copy import deepcopy
from contextlib import contextmanager
from datetime import timedelta
import hashlib
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.db import connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import User
from roo.models import Ledger
from workflow_runs.models import ContentFactoryRun
from . import founder_run_commands as commands
from . import tests_run_current_review as paired
from .founder_journey_testing import ALL_P1_FLAGS_ON
from .models import OrganizationContentConfig
from .run_reconciliation import reconcile_authoritative_run
from .run_verified_delivery import project_verified_delivery
from .self_heal_testing import make_p2_org
from .tests_run_event_authority import FLAGS, advance_ticket


def authenticated_client(user):
    from community_chat.account_sessions import issue_account_session
    from community_chat.models import CommunityMemberProfile
    CommunityMemberProfile.objects.update_or_create(user=user, defaults={"status": "approved"})
    credentials = issue_account_session(user, SimpleNamespace(client_id="mlai-chat-ios",
        installation_id=uuid4(), origin="mlaichat://callback", platform="ios", device_name="Synthetic command test",
        public_key=hashlib.sha256(f"synthetic-control:{user.pk}".encode()).hexdigest()))
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {credentials.access_token}")
    return client


@override_settings(**FLAGS, **ALL_P1_FLAGS_ON, ARTICLES_FREE_FIRST_ARTICLE_ENABLED=True,
    CONTENT_FACTORY_URL="https://cf.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-paired-key")
class FounderRunCommandTests(TransactionTestCase):
    fixture_name = "r3_owned_portable_review_approval.json"
    reads = paired.CurrentReviewTests.reads

    def setUp(self):
        paired.CurrentReviewTests.setUp(self)
        cache.clear()
        self.client = authenticated_client(self.user)
        with self.reads():
            reconcile_authoritative_run(self.run.run_id, refresh=True)
            project_verified_delivery(self.run.run_id)
        self.run.refresh_from_db()
        self.revision = self.fixture["article_review_response"]["revision"]
        self.body = {"operationId": "original-control-command", "reviewedRevision": self.revision,
            "waivedComments": [], "acceptDisplayedRevision": True}
        self.before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        self.ledgers = list(Ledger.objects.values())

    def post(self, action, body=None, key="retained-control-key", *, client=None, run_id=None):
        return (client or self.client).post(
            f"/api/v1/my-startup/vibe-marketing/runs/{run_id or self.run.run_id}/{action}/?company_id={self.company.pk}",
            self.body if body is None else body, format="json", **({"HTTP_IDEMPOTENCY_KEY": key} if key is not None else {}))

    @contextmanager
    def remote(self, action="approve", **options):
        value = self.fixture["approval_response"] if action == "approve" else {
            "run_id": self.run.run_id, "status": "denied" if action == "deny" else "cancelled"}
        with self.reads(), patch("content_factory.vibe_marketing_views.http_client.request",
                return_value=paired.response(self.fixture["article_review_response"])), \
                patch("content_factory.vibe_marketing_views.http_client.post",
                    **(options or {"return_value": paired.response(value)})) as outbound:
            yield outbound

    def receipt(self):
        strategy = OrganizationContentConfig.objects.get(organization=self.org).pillar_strategy
        return next(iter(strategy[commands.KEY].values()))

    def assert_no_execution_or_billing_writes(self):
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for field in ("status", "approval_state", "run_request", "created_at", "updated_at",
                      "last_event_emitted_at", "reconciled_at"):
            self.assertEqual(after[field], self.before[field], field)
        self.assertEqual(list(Ledger.objects.values()), self.ledgers)

    def test_approve_replays_before_prior_consent_but_changed_body_or_action_conflicts(self):
        with self.remote() as outbound:
            first, replay = self.post("approve"), self.post("approve")
            changed = self.post("approve", {**self.body, "reviewedRevision": "different-revision"})
            action = self.post("cancel")
        self.assertEqual((first.status_code, replay.status_code, changed.status_code, action.status_code),
            (202, 202, 409, 409), [r.data for r in (first, replay, changed, action)])
        self.assertEqual(outbound.call_count, 1)
        receipt = self.receipt()
        self.assertEqual(receipt["state"], "accepted")
        self.assertNotIn("response", receipt)
        self.assertNotIn("payload", receipt)
        self.assert_no_execution_or_billing_writes()

    def _stop_replay(self, action):
        with self.remote(action) as outbound:
            first, replay = self.post(action), self.post(action)
            changed = self.post(action, {**self.body, "reviewedRevision": "different-revision"})
        self.assertEqual([x.status_code for x in (first, replay, changed)], [202, 202, 409], first.data)
        self.assertEqual(outbound.call_count, 1)
        self.assert_no_execution_or_billing_writes()

    def test_deny_accepted_replays_without_another_cf_post(self): self._stop_replay("deny")
    def test_cancel_accepted_replays_without_another_cf_post(self): self._stop_replay("cancel")

    def test_new_key_cannot_reuse_prior_consent_for_the_wrong_revision_or_waiver(self):
        with self.remote() as outbound:
            self.assertEqual(self.post("approve").status_code, 202)
            wrong = self.post("approve", {**self.body, "reviewedRevision": "stale"}, key="other-key")
            waiver = self.post("approve", {**self.body, "waivedComments": [{"id": "unseen", "body": "new"}]}, key="third-key")
            waiver_replay = self.post("approve", {**self.body, "waivedComments": [{"id": "unseen", "body": "new"}]}, key="third-key")
        self.assertEqual((wrong.status_code, waiver.status_code, waiver_replay.status_code), (409, 409, 409))
        self.assertEqual(outbound.call_count, 1)

    def test_unknown_cancel_response_never_reposts_even_long_after_initial_request(self):
        import requests
        with self.remote("cancel", side_effect=requests.Timeout("Synthetic acknowledgement loss")) as outbound:
            first = self.post("cancel")
            with patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(days=365)):
                replay = self.post("cancel", client=authenticated_client(self.user))
        self.assertIn(first.status_code, (502, 503))
        self.assertEqual(replay.status_code, 503, replay.data)
        self.assertEqual(outbound.call_count, 1)
        self.assertEqual(self.receipt()["state"], "dispatching")
        self.assert_no_execution_or_billing_writes()

    def test_malformed_success_cannot_become_a_replayable_acceptance(self):
        with self.remote("cancel", return_value=paired.response({"status": "cancelled", "run_id": "wrong-run"})) as outbound:
            first, replay = self.post("cancel"), self.post("cancel")
        self.assertEqual((first.status_code, replay.status_code), (503, 503))
        self.assertEqual(outbound.call_count, 1)
        self.assert_no_execution_or_billing_writes()

    def test_conflicting_optional_ticket_or_explicit_rejection_is_not_acceptance(self):
        from .run_ownership import WIRE_EXECUTION_KEY
        for i, extra in enumerate(({WIRE_EXECUTION_KEY: {"execution_id": "other"}}, {"allowed": False},
                                  {"accepted": False}, {"action": "deny"})):
            value = {"status": "cancelled", "run_id": self.run.run_id, **extra}
            with self.subTest(extra=extra), self.remote("cancel", return_value=paired.response(value)) as outbound:
                first = self.post("cancel", key=f"malformed-{i}")
                replay = self.post("cancel", key=f"malformed-{i}")
            self.assertEqual((first.status_code, replay.status_code), (503, 503))
            self.assertEqual(outbound.call_count, 1)

    def test_lost_client_response_after_durable_acceptance_replays_without_another_post(self):
        finish = commands._finish
        def saved_then_lost(*args):
            finish(*args)
            raise RuntimeError("Synthetic client response loss after receipt commit")
        self.client.raise_request_exception = False
        with self.remote("cancel") as outbound:
            with patch.object(commands, "_finish", side_effect=saved_then_lost):
                lost = self.post("cancel")
            replay = self.post("cancel")
        self.assertEqual((lost.status_code, replay.status_code), (500, 202), replay.data)
        self.assertEqual(outbound.call_count, 1)
        self.assert_no_execution_or_billing_writes()

    def test_simultaneous_same_key_has_only_one_external_submission(self):
        entered, released = Event(), Event()
        results, errors = [], []
        def submit(*args, **kwargs):
            entered.set()
            if not released.wait(timeout=20): raise RuntimeError("Concurrent test did not release transport")
            return paired.response({"run_id": self.run.run_id, "status": "cancelled"})
        def worker():
            try:
                client = APIClient()
                client.credentials(**self.client._credentials)
                results.append(self.post("cancel", client=client))
            except Exception as exc:
                errors.append(repr(exc))
            finally:
                connections.close_all()
        with self.remote("cancel", side_effect=submit) as outbound:
            thread = Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(entered.wait(timeout=15))
                pending = self.post("cancel")
                self.assertEqual(pending.status_code, 503, pending.data)
            finally:
                released.set()
                thread.join(timeout=25)
            replay = self.post("cancel")
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual([r.status_code for r in results], [202])
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(outbound.call_count, 1)

    def test_new_ticket_cannot_replay_the_original_acceptance(self):
        with self.remote("cancel") as outbound:
            self.assertEqual(self.post("cancel").status_code, 202)
            advance_ticket(self.run)
            replay = self.post("cancel")
        self.assertEqual(replay.status_code, 409, replay.data)
        self.assertEqual(outbound.call_count, 1)

    def test_ticket_changed_after_payload_build_cannot_receive_the_old_command(self):
        before = commands.before_transport
        def changed(**kwargs):
            advance_ticket(self.run)
            return before(**kwargs)
        with self.remote("cancel") as outbound, patch.object(commands, "before_transport", side_effect=changed):
            response = self.post("cancel")
        self.assertEqual(response.status_code, 409, response.data)
        outbound.assert_not_called()

    def test_current_account_revocation_is_checked_again_at_transport(self):
        before = commands.before_transport
        def revoked(**kwargs):
            User.objects.filter(pk=self.user.pk).update(is_active=False)
            return before(**kwargs)
        with self.remote("cancel") as outbound, patch.object(commands, "before_transport", side_effect=revoked):
            response = self.post("cancel")
        self.assertEqual(response.status_code, 403, response.data)
        outbound.assert_not_called()
        self.assert_no_execution_or_billing_writes()

    def test_real_session_revocation_prevents_accepted_replay(self):
        with self.remote("cancel") as outbound:
            self.assertEqual(self.post("cancel").status_code, 202)
            User.objects.filter(pk=self.user.pk).update(is_active=False)
            replay = self.post("cancel")
        self.assertEqual(replay.status_code, 401, replay.data)
        self.assertEqual(outbound.call_count, 1)

    def test_foreign_principal_cannot_replay_an_accepted_command(self):
        foreign = authenticated_client(make_p2_org(domain="other-control.example").owner)
        with self.remote("cancel") as outbound:
            self.assertEqual(self.post("cancel").status_code, 202)
            replay = self.post("cancel", client=foreign)
        self.assertIn(replay.status_code, (403, 404), replay.data)
        self.assertEqual(outbound.call_count, 1)

    def test_unkeyed_controls_preserve_the_existing_transport_path(self):
        with self.remote("cancel") as outbound:
            first, second = self.post("cancel", key=None), self.post("cancel", key=None)
        self.assertEqual((first.status_code, second.status_code), (202, 202))
        self.assertEqual(outbound.call_count, 2)
        self.assertNotIn(commands.KEY, OrganizationContentConfig.objects.get(organization=self.org).pillar_strategy)

    def test_same_header_cannot_target_another_run_in_the_same_startup(self):
        from .run_ownership import reserve_admitted_run, bind_admitted_run, user_principal, WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY
        raw = {k: deepcopy(v) for k, v in self.run.run_request.items() if k not in {WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY}}
        raw["client_request_id"] = "separate-original-control"
        other = bind_admitted_run(reserve_admitted_run(organization=self.org,
            principal=user_principal(self.user.pk, f"mlai_user:{self.user.pk}"),
            workflow="article_generation", payload=raw), "separate-control-worker")
        with self.remote("cancel") as outbound:
            self.assertEqual(self.post("cancel").status_code, 202)
            replay = self.post("cancel", run_id=other.run_id)
        self.assertEqual(replay.status_code, 409, replay.data)
        self.assertEqual(outbound.call_count, 1)

    def test_current_frozen_team_refuses_even_the_original_owner_replay(self):
        from founder_tools import startup_team
        with self.remote("cancel") as outbound:
            self.assertEqual(self.post("cancel").status_code, 202)
            with startup_team._locked(self.org, create=True) as (config, roster):
                roster["frozen"] = True
                startup_team._save(config, roster)
            replay = self.post("cancel")
        self.assertIn(replay.status_code, (403, 409), replay.data)
        self.assertEqual(outbound.call_count, 1)

    def test_accepted_replay_reads_the_original_current_projection(self):
        with self.remote("cancel") as outbound:
            first = self.post("cancel")
            self.assertEqual(first.status_code, 202, first.data)
            ContentFactoryRun.objects.filter(pk=self.run.pk).update(current_step="newer_observation")
            replay = self.post("cancel")
        self.assertEqual(replay.status_code, 202, replay.data)
        self.assertEqual(replay.data["currentStep"], "newer_observation")
        self.assertEqual(outbound.call_count, 1)

    def test_unknown_approval_or_denial_does_not_retry_provider_work(self):
        import requests
        for action in ("approve", "deny"):
            with self.subTest(action=action), self.remote(action, side_effect=requests.Timeout("Synthetic unknown effect")) as outbound:
                first = self.post(action, key=action + "-unknown")
                replay = self.post(action, key=action + "-unknown")
            self.assertIn(first.status_code, (502, 503), first.data)
            self.assertEqual(replay.status_code, 503, replay.data)
            self.assertEqual(outbound.call_count, 1)
        self.assert_no_execution_or_billing_writes()

    def test_keyed_group_cancel_is_not_a_single_run_command(self):
        with self.remote("cancel") as outbound:
            result = self.post("cancel", {**self.body, "cancelGroup": True})
        self.assertEqual(result.status_code, 400, result.data)
        outbound.assert_not_called()

    def test_key_validation_precedes_external_effects(self):
        with self.remote("cancel") as outbound:
            response = self.post("cancel", key=" invalid whitespace ")
        self.assertEqual(response.status_code, 400, response.data)
        outbound.assert_not_called()
