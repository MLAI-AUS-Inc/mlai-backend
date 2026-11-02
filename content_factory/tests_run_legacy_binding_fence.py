"""Legacy token collisions cannot merge or erase an admitted execution."""
from django.test import TransactionTestCase, override_settings

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep, ContentFactoryRunStepAttempt
from .dispatch_binding import bind_dispatch_token_run, _merge_provisional_into_real
from .models import ContentFactoryJob
from .run_backend_facts import create_job_facts
from .run_event_authority import receive_authoritative_run_events
from .run_ownership import RunOwnershipConflict, bind_admitted_run, reserve_admitted_run, user_principal
from .tests_run_event_authority import FLAGS, batch


@override_settings(**FLAGS)
class LegacyBindingOwnerFenceTests(TransactionTestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic binder owner", domain="binding-owner.example")
        self.other = Organization.objects.create(name="Synthetic other tenant", domain="binding-other.example")
        self.owned = reserve_admitted_run(organization=self.org, workflow="article_generation",
            principal=user_principal(123, "user:123"), payload={"domain": self.org.domain, "client_request_id": "owned-original"})
        self.owned = bind_admitted_run(self.owned, "already-owned-worker")
        self.assertEqual(receive_authoritative_run_events(batch(self.owned, [
            {"status": "running"}, {"type": "step_started", "step": "write", "attempt": 1}])).status_code, 200)
        create_job_facts(job_id=self.owned.run_id, domain=self.org.domain, slack_user_id="user:123", status="generating")

    def legacy(self, *, organization=None, key="legacy-token", repo="synthetic/foreign-carrier"):
        org = organization or self.org
        run = ContentFactoryRun.objects.create(run_id=key, organization=org, domain=org.domain,
            workflow="article_generation", github_repo=repo, status="blocked", current_step="dispatch_pending",
            slack_user_id="legacy:actor", run_request={"client_request_id": key})
        ContentFactoryJob.objects.create(job_id=key, domain=org.domain, slack_user_id="legacy:actor",
                                        client_request_id=key, status="queued")
        return run

    def snapshot(self):
        return {
            "runs": list(ContentFactoryRun.objects.order_by("pk").values()),
            "steps": list(ContentFactoryRunStep.objects.order_by("pk").values()),
            "attempts": list(ContentFactoryRunStepAttempt.objects.order_by("pk").values()),
            "jobs": list(ContentFactoryJob.objects.order_by("pk").values()),
        }

    def test_same_tenant_legacy_token_cannot_merge_into_owned_target_or_change_either_history(self):
        token = self.legacy()
        before = self.snapshot()
        self.assertIsNone(bind_dispatch_token_run(client_request_id=token.run_id, remote_run_id=self.owned.run_id))
        self.assertEqual(self.snapshot(), before)

    def test_foreign_tenant_legacy_token_cannot_merge_or_delete_owned_target(self):
        token = self.legacy(organization=self.other)
        before = self.snapshot()
        self.assertIsNone(bind_dispatch_token_run(client_request_id=token.run_id, remote_run_id=self.owned.run_id))
        self.assertEqual(self.snapshot(), before)

    def test_switches_off_still_protect_existing_target_receipt(self):
        token = self.legacy()
        before = self.snapshot()
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=False,
                               ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False):
            self.assertIsNone(bind_dispatch_token_run(client_request_id=token.run_id, remote_run_id=self.owned.run_id))
        self.assertEqual(self.snapshot(), before)

    def test_owned_token_and_equal_identity_are_never_legacy_rebound(self):
        before = self.snapshot()
        self.assertIsNone(bind_dispatch_token_run(client_request_id=self.owned.run_id, remote_run_id=self.owned.run_id))
        self.assertIsNone(bind_dispatch_token_run(client_request_id=self.owned.run_id, remote_run_id="replacement-worker"))
        self.assertEqual(self.snapshot(), before)

    def test_merge_helper_itself_refuses_both_owned_source_and_target(self):
        legacy = self.legacy()
        before = self.snapshot()
        for source, target in ((legacy, self.owned), (self.owned, legacy)):
            with self.subTest(source=source.run_id), self.assertRaises(RunOwnershipConflict):
                _merge_provisional_into_real(source, target)
        self.assertEqual(self.snapshot(), before)

    def test_unmarked_legacy_collision_keeps_original_compatible_merge_behavior(self):
        token = self.legacy()
        target = ContentFactoryRun.objects.create(run_id="legacy-real-worker", organization=self.org,
            domain=self.org.domain, workflow="article_generation", status="running")
        result = bind_dispatch_token_run(client_request_id=token.run_id, remote_run_id=target.run_id)
        self.assertEqual(result.pk, target.pk)
        self.assertFalse(ContentFactoryRun.objects.filter(pk=token.pk).exists())
        target.refresh_from_db()
        self.assertEqual(target.github_repo, token.github_repo)
        self.assertEqual(target.run_request["client_request_id"], token.run_id)
        self.assertEqual(target.status, "running")
        self.assertTrue(ContentFactoryJob.objects.filter(job_id=target.run_id).exists())

    def test_unmarked_legacy_rename_keeps_canonical_pk_and_step_history(self):
        token = self.legacy()
        step = ContentFactoryRunStep.objects.create(run=token, step_key="legacy-history", attempts=1)
        attempt = ContentFactoryRunStepAttempt.objects.create(step=step, attempt=1, status="completed")
        result = bind_dispatch_token_run(client_request_id=token.run_id, remote_run_id="new-legacy-worker")
        self.assertEqual((result.pk, result.run_id, result.status), (token.pk, "new-legacy-worker", "queued"))
        self.assertTrue(ContentFactoryRunStep.objects.filter(pk=step.pk, run_id=token.pk).exists())
        self.assertTrue(ContentFactoryRunStepAttempt.objects.filter(pk=attempt.pk).exists())
