"""Synthetic ledger persistence/auth races in the existing disposable CI lane.

Do not run locally without separate migration approval. Adds no migration files.
"""
from copy import deepcopy
from threading import Barrier, Thread
from uuid import uuid4
from django.db import close_old_connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from tests.test_content_island_bootstrap import ContentIslandBootstrapTestCase
from . import website_cost_ledger as ledger, website_goal as goals
from .tests_website_connections import WebsiteDatabaseFixture
from .tests_website_cost_usage_unit import accounting
from .tests_website_usage_settlement_unit import report
from .website_connections import summary_for
from .website_models import WebsiteConnection, WebsiteConnectionOperation


def seed(website, company_id, identifier):
    scope = goals.scope_for(website, company_id)
    parent = WebsiteConnectionOperation.objects.create(connection=website, generation=website.generation,
        action="prepare", state="completed", idempotency_key="goal:" + identifier,
        payload={"company_id": str(company_id)})
    parent.receipt = {"goal": goals.new_goal(str(parent.pk), scope, now=timezone.now())}
    parent.save(update_fields=["receipt"])
    usage = report(run_id=identifier, tokens=17, provider_seconds=0.2, active_seconds=3, model_usd=0.00017)
    usage["accounting"] = accounting(usage)
    run = ContentFactoryRun.objects.create(run_id=identifier, organization=website.organization,
        domain=website.organization.domain, workflow="article_system_setup", status="completed",
        run_request={"website_connection_id": str(website.pk), "connection_generation": website.generation,
            "repository_id": website.repository_id, "branch": website.branch, "app_root": website.app_root,
            "deletion_epoch": scope["deletion_epoch"], "goal_budget": {"version": 1, "goal_id": str(parent.pk), "scope_digest": goals.digest(scope)}},
        result={"usage": usage})
    return parent, run


@override_settings(ARTICLES_COST_ACCOUNTING_ENABLED=True)
class LedgerPersistenceTests(WebsiteDatabaseFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.company_id = str(uuid4())
        self.parent, self.run = seed(self.website, self.company_id, "original-cost-run")

    def read(self, **kwargs):
        return ledger.read(company_id=self.company_id, organization_id=self.org.pk, **kwargs)

    def test_replay_reuse_and_out_of_order_observations_count_one_original_run(self):
        first = ledger.record(self.run)
        self.assertEqual(ledger.record(self.run).pk, first.pk)
        replay_time = first.updated_at
        first.refresh_from_db()
        self.assertEqual(first.updated_at, replay_time)
        later = deepcopy(self.run)
        later.result["usage"]["state_version"] = 2
        later.result["usage"]["tokens"] = 20
        later.result["usage"]["accounting"]["tokens"].update(total=20, known_subtotal=20)
        ledger.record(later)
        ledger.record(self.run)
        self.assertEqual(self.read()["metrics"]["tokens"]["total"], 20)
        self.assertEqual(self.read()["recordedRuns"], 1)
        self.assertEqual(self.read(goal_id=str(self.parent.pk))["recordedRuns"], 1)
        self.assertEqual(self.read(goal_id=str(uuid4()))["recordedRuns"], 0)
        latest = summary_for(self.config, company_id=self.company_id)["latestOperation"]
        self.assertEqual(latest["id"], str(self.parent.pk))
        self.assertEqual(WebsiteConnectionOperation.objects.filter(action=ledger.ACTION).count(), 1)

    def test_racing_snapshot_insert_and_update_retains_the_newest_identity(self):
        newer = deepcopy(self.run)
        newer.result["usage"]["state_version"] = 2
        newer.result["usage"]["tokens"] = 20
        newer.result["usage"]["accounting"]["tokens"].update(total=20, known_subtotal=20)
        barrier, errors = Barrier(2), []
        def write(value):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                ledger.record(value)
            except Exception as exc:
                errors.append(exc)
            finally:
                close_old_connections()
        workers = [Thread(target=write, args=(value,)) for value in (self.run, newer)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(self.read()["recordedRuns"], 1)
        self.assertEqual(self.read()["metrics"]["tokens"]["total"], 20)

    def test_reconnect_retains_history_and_erasure_cannot_recreate_it(self):
        ledger.record(self.run)
        self.website.generation += 1
        self.website.state = "disconnected"
        self.website.save(update_fields=["generation", "state"])
        self.assertEqual(self.read()["recordedRuns"], 1)
        self.assertEqual(ledger.read(company_id=str(uuid4()), organization_id=self.org.pk)["recordedRuns"], 0)
        self.website.delete()
        self.assertIsNone(ledger.record(self.run))
        self.assertEqual(self.read()["recordedRuns"], 0)


@override_settings(ARTICLES_COST_ACCOUNTING_ENABLED=True)
class CostUsageAuthorizationTests(ContentIslandBootstrapTestCase):
    def setUp(self):
        super().setUp()
        self.website = WebsiteConnection.objects.create(organization=self.organization, github_repo="fixture/site",
            repository_id=500, branch="main")
        self.config.website_connection = self.website
        self.config.save(update_fields=["website_connection"])
        self.parent, run = seed(self.website, self.company.pk, "owned-cost-run")
        ledger.record(run)

    def test_legacy_and_chat_reads_enforce_company_ownership_and_internal_receipt_privacy(self):
        from django.contrib.auth import get_user_model
        other_user = get_user_model().objects.create_user(email="other-cost-owner@example.test")
        other_profile = VibeRaisingProfile.objects.create(user=other_user, role="founder")
        other_company = VibeRaisingCompany.objects.create(profile=other_profile, organization=self.organization,
            name="Other founder", domain=self.organization.domain)
        row = WebsiteConnectionOperation.objects.get(action=ledger.ACTION)
        for prefix in ("/api/v1", "/api/v1/my-startup"):
            url = prefix + "/vibe-marketing/website-connection/usage/"
            response = self.client.get(url, {"company_id": str(self.company.pk)})
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response["Cache-Control"], "private, no-store")
            self.assertEqual(response.data["usage"]["recordedRuns"], 1)
            self.assertEqual(response.data["usage"]["metrics"]["tokens"]["total"], 17)
            denied = self.client.get(url, {"company_id": str(other_company.pk)})
            self.assertEqual(denied.status_code, 404, denied.data)
            hidden = self.client.get(prefix + f"/vibe-marketing/website-connection/operations/{row.pk}/",
                {"company_id": str(self.company.pk)})
            self.assertEqual(hidden.status_code, 404, hidden.data)
