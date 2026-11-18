"""quiesce_self_heal_history and replay_stopped: dry-run first, idempotent, inside the caps (A16)."""
from datetime import timedelta
from io import StringIO
import json
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from workflow_runs.models import ContentFactoryRun
from . import founder_journey_alerts as alerts
from .founder_journey_testing import _connect, make_history_free_org
from .models import OrganizationContentConfig
from .website_models import WebsiteConnectionOperation

SCHEDULER = {"request_source": "mlai_backend_scheduler", "include_expansion": False}


def call(name, *args):
    out = StringIO()
    call_command(name, *args, "--json", stdout=out)
    return json.loads(out.getvalue())


class QuiesceSelfHealHistoryTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        settings = override_settings(ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE=(self.now - timedelta(days=30)).date().isoformat())
        settings.enable()
        self.addCleanup(settings.disable)
        self.org = make_history_free_org().organization

    def make(self, *, request=None, result=None, workflow="island_refresh", status="failed", age=timedelta(days=1)):
        run = ContentFactoryRun.objects.create(run_id=f"run-{uuid4().hex[:12]}", organization=self.org,
            domain=self.org.domain, workflow=workflow, status=status, run_request=request or {}, result=result or {})
        ContentFactoryRun.objects.filter(pk=run.pk).update(created_at=self.now - age, updated_at=self.now - age)
        run.refresh_from_db()
        return run

    def recoded(self, **kwargs):
        receipt = {"version": 1, "mode": "policy", "code": "ISLAND_KEYWORDS_EMPTY", "attempts": 2,
                   "last_action": "stop", "last_action_at": (self.now - timedelta(hours=5)).isoformat(), "capped": True,
                   "history": [{"action": "retry", "code": "ISLAND_KEYWORDS_EMPTY", "attempt": 1}]}
        return self.make(result={"error_code": "SELF_HEAL_DEADLINE_REACHED", "self_heal_v1": receipt,
                                "failure": {"code": "ISLAND_KEYWORDS_EMPTY"}}, **kwargs)

    def test_dry_run_is_the_default_and_writes_nothing(self):
        swept = self.recoded(request=SCHEDULER)
        unswept = self.make(request=SCHEDULER, result={"error_code": "ISLAND_KEYWORDS_EMPTY"})
        before = {run.pk: (run.result, run.updated_at) for run in ContentFactoryRun.objects.all()}
        report = call("quiesce_self_heal_history")
        self.assertEqual(report["mode"], "dry_run")
        self.assertEqual((report["stamped"], report["restored"]), (1, 1))
        self.assertEqual({row["run_pk"] for row in report["rows"]}, {swept.pk, unswept.pk})
        self.assertEqual({run.pk: (run.result, run.updated_at) for run in ContentFactoryRun.objects.all()}, before)

    def test_apply_stamps_restores_keeps_updated_at_and_is_idempotent(self):
        swept = self.recoded(request=SCHEDULER)
        unswept = self.make(request=SCHEDULER, result={"error_code": "ISLAND_KEYWORDS_EMPTY"})
        legacy = self.recoded()  # an old nightly refresh saved before the request_source stamp
        no_original = self.make(request=SCHEDULER, result={"error_code": "SELF_HEAL_ATTEMPTS_EXHAUSTED",
                                                          "self_heal_v1": {"last_action": "stop"}})
        founder = self.recoded(request={"request_source": "roo_slackbot", "client_request_id": "island-research:x"})
        old_founder = self.make(workflow="article_generation", request={"request_source": "roo_slackbot"},
                               result={"error_code": "REVIEW_PROVIDER_UNAVAILABLE"}, age=timedelta(days=90))
        new_founder = self.make(workflow="article_generation", request={"request_source": "roo_slackbot"},
                               result={"error_code": "REVIEW_PROVIDER_UNAVAILABLE"})
        clocks = {run.pk: run.updated_at for run in ContentFactoryRun.objects.all()}
        report = call("quiesce_self_heal_history", "--apply")
        self.assertEqual(report["mode"], "apply")
        for run in (swept, legacy):
            run.refresh_from_db()
            self.assertEqual(run.result["error_code"], "ISLAND_KEYWORDS_EMPTY")
            self.assertEqual(run.result["self_heal_v1"]["stop"]["code"], "SELF_HEAL_DEADLINE_REACHED")
            self.assertEqual(run.result["self_heal_v1"]["last_action"], "stop")
        unswept.refresh_from_db()
        self.assertEqual(unswept.result["self_heal_v1"]["last_action"], "none")
        no_original.refresh_from_db()
        self.assertEqual(no_original.result["error_code"], "SELF_HEAL_ATTEMPTS_EXHAUSTED")
        self.assertEqual(no_original.result["self_heal_v1"]["quiesced_v1"]["unrestorable"], "SELF_HEAL_ATTEMPTS_EXHAUSTED")
        old_founder.refresh_from_db()
        self.assertEqual(old_founder.result["self_heal_v1"]["last_action"], "none")
        self.assertEqual(old_founder.result["error_code"], "REVIEW_PROVIDER_UNAVAILABLE")
        # A founder run self-heal already handled, and a recent untouched one, are not this command's.
        for run in (founder, new_founder):
            before = run.result
            run.refresh_from_db()
            self.assertEqual(run.result, before)
        self.assertEqual({run.pk: run.updated_at for run in ContentFactoryRun.objects.all()}, clocks)
        from . import failure_policy_engine as engine
        self.assertNotIn(old_founder.pk, {run.pk for run in engine._candidates(self.now, 500)})
        snapshot = {run.pk: run.result for run in ContentFactoryRun.objects.all()}
        again = call("quiesce_self_heal_history", "--apply")
        self.assertEqual((again["stamped"], again["restored"], again["unrestorable"], again["rows"]), (0, 0, 0, []))
        self.assertEqual({run.pk: run.result for run in ContentFactoryRun.objects.all()}, snapshot)

    def test_pending_scheduler_receipts_are_counted_not_changed(self):
        nightly = self.make(request=SCHEDULER)
        strategy = {alerts.RECEIPTS_KEY: {"a": {"alert_id": "a", "state": "pending", "run_pk": nightly.pk},
                                          "b": {"alert_id": "b", "state": "sent", "run_pk": nightly.pk}}}
        OrganizationContentConfig.objects.filter(organization=self.org).update(pillar_strategy=strategy)
        report = call("quiesce_self_heal_history", "--apply")
        self.assertEqual(report["pending_scheduler_receipts"], 1)
        self.assertEqual(OrganizationContentConfig.objects.get(organization=self.org).pillar_strategy, strategy)


class ReplayStoppedTests(TestCase):
    CODE = "article_surface_unresolved"

    def setUp(self):
        cache.clear()
        self.built = make_history_free_org()
        self.org, self.config = self.built.organization, self.built.config
        self.connection, _ = _connect(self.org, self.config, repo="example-replay/website")
        self.connection.authorized_by = self.built.owner
        self.connection.save(update_fields=["authorized_by"])
        self.config.refresh_from_db()

    def stopped(self, *, code=None, state="needs_attention", generation=None, connection=None, receipt=None):
        connection = connection or self.connection
        generation = generation or connection.generation
        return WebsiteConnectionOperation.objects.create(
            connection=connection, generation=generation, action="prepare", state=state,
            idempotency_key=f"{connection.pk}:prepare:{generation}",
            payload={"company_id": str(self.built.company.pk), "attempt": 1, "user_clicks": 1},
            receipt={"status": "stopped", "reason_code": code or self.CODE, "current_step": "setup",
                     "message": "We could not find the articles page.", **(receipt or {})})

    def test_dry_run_is_the_default_and_changes_nothing(self):
        op = self.stopped()
        before = (op.state, op.receipt, op.updated_at)
        report = call("replay_stopped", "--code", self.CODE)
        self.assertEqual((report["mode"], report["eligible"], report["replayed"]), ("dry_run", 1, 0))
        op.refresh_from_db()
        self.assertEqual((op.state, op.receipt, op.updated_at), before)

    def test_apply_reopens_through_try_again_once(self):
        op = self.stopped()
        with patch("content_factory.website_prepare.advance_prepare") as advance:
            report = call("replay_stopped", "--code", self.CODE, "--apply")
        advance.assert_not_called()
        self.assertEqual(report["replayed"], 1, report)
        op.refresh_from_db()
        self.assertEqual(op.state, "pending")
        self.assertEqual(op.receipt["reason_code"], "")
        self.assertEqual(op.payload["user_clicks"], 2)
        self.assertEqual(op.receipt["replay_v1"]["stopped_on"], self.CODE)
        self.assertEqual(op.receipt["replay_v1"]["from_state"], "needs_attention")
        # The same code stopping it again later is not replayed twice by the same release.
        WebsiteConnectionOperation.objects.filter(pk=op.pk).update(
            state="needs_attention", receipt={**op.receipt, "reason_code": self.CODE})
        again = call("replay_stopped", "--code", self.CODE, "--apply")
        self.assertEqual((again["replayed"], again["skipped"]), (0, {"already_replayed": 1}))

    def test_only_current_generation_connected_ops_within_the_caps_reopen(self):
        from . import website_prepare_setup_cap as setup_cap
        cases = {}
        # Other code: never selected.
        other = self.stopped(code="SOMETHING_ELSE")
        # An older generation of the same connection.
        old = WebsiteConnectionOperation.objects.create(connection=self.connection, generation=self.connection.generation,
            action="prepare", state="needs_attention", idempotency_key=f"{self.connection.pk}:prepare:old",
            receipt={"reason_code": self.CODE})
        self.connection.__class__.objects.filter(pk=self.connection.pk).update(generation=self.connection.generation + 1)
        cases["old_generation"] = old
        report = call("replay_stopped", "--code", self.CODE)
        self.assertEqual(report["skipped"], {"not_current_connected_generation": 1})
        self.assertNotIn(str(other.pk), {row["operation_id"] for row in report["rows"]})

        # A disconnected website.
        self.connection.refresh_from_db()
        capped = self.stopped(receipt={setup_cap.RECEIPT_KEY: ["s1", "s2", "s3"]})
        report = call("replay_stopped", "--code", self.CODE)
        self.assertEqual(report["skipped"].get("setup_cap"), 1)
        self.connection.__class__.objects.filter(pk=self.connection.pk).update(state="disconnected")
        report = call("replay_stopped", "--code", self.CODE, "--apply")
        self.assertEqual(report["replayed"], 0)
        self.assertEqual(report["skipped"].get("not_current_connected_generation"), 2)
        for op in (other, old, capped):
            state = op.state
            op.refresh_from_db()
            self.assertEqual(op.state, state)

    def test_replay_keeps_the_founders_goal_target(self):
        # Re-opening a first-article goal as "ready" would supersede (cancel and
        # refund) the founder's goal instead of retrying it.
        op = self.stopped(state="needs_user", receipt={"goal": {"target": "first_article_live"}})
        targets = []

        def reopen(config, **kwargs):
            targets.append(kwargs["target"])
            WebsiteConnectionOperation.objects.filter(pk=op.pk).update(state="pending", receipt={**op.receipt, "reason_code": ""})
            return WebsiteConnectionOperation.objects.get(pk=op.pk)

        with patch("content_factory.website_prepare.start_prepare", side_effect=reopen) as start:
            report = call("replay_stopped", "--code", self.CODE, "--apply")
        self.assertEqual(report["replayed"], 1, report)
        self.assertEqual(targets, ["first_article_live"])
        self.assertIs(start.call_args.kwargs["retry"], True)

    def test_pull_request_cap_holds_the_replay(self):
        op = self.stopped()
        with patch("content_factory.failure_policy_engine.pull_request_cap_reached", return_value=True):
            report = call("replay_stopped", "--code", self.CODE, "--apply")
        self.assertEqual((report["replayed"], report["skipped"]), (0, {"pull_request_cap": 1}))
        op.refresh_from_db()
        self.assertEqual(op.state, "needs_attention")

    def test_bad_input_is_refused(self):
        from django.core.management.base import CommandError
        for args in (("--code", "not a code"), ("--code", self.CODE, "--since", "yesterday"),
                     ("--code", self.CODE, "--limit", "0")):
            with self.subTest(args=args), self.assertRaises(CommandError):
                call("replay_stopped", *args)
