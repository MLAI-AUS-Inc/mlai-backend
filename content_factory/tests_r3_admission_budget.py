"""Freeze backend-only continuation budgets before the first admission hash."""
from copy import deepcopy

from django.db import transaction
from django.test import TestCase, override_settings

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun

from .run_ownership import (
    BUDGET_KEY, WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY, RunOwnershipConflict,
    owner_receipt, public_run_value, reserve_admitted_run, seal_new_run_admission, user_principal,
)
from .website_contract import evidence_digest


BUDGET = {"schema_version": 1, "runtime": "article", "max_model_usd": "5.00", "max_model_tokens": 50000}


@override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=True, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=True,
                   ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=True,
                   ARTICLES_RUN_CONTROL_MODEL_USD="5.00", ARTICLES_RUN_CONTROL_MODEL_TOKENS=50000)
class AdmissionBudgetTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic budget owner", domain="budget-r3.example")
        self.principal = user_principal(123, "user:123")

    def payload(self, **values):
        return {"domain": self.org.domain, "client_request_id": "budget-original", "topic": "Retained body", **values}

    def reserve(self, payload, *, workflow="article_generation"):
        return reserve_admitted_run(organization=self.org, principal=self.principal, workflow=workflow, payload=payload)

    def test_server_cap_is_frozen_before_both_admission_and_execution_request_hashes(self):
        payload = self.payload()
        run = self.reserve(payload)
        self.assertEqual(payload[BUDGET_KEY], BUDGET)
        self.assertEqual(run.run_request[BUDGET_KEY], BUDGET)
        original = {key: value for key, value in run.run_request.items() if key not in {WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY}}
        expected = evidence_digest(original)
        self.assertEqual(owner_receipt(run)["identity"]["request_digest"], expected)
        self.assertEqual(payload[WIRE_EXECUTION_KEY]["request_digest"], expected)
        self.assertNotEqual(expected, evidence_digest({key: value for key, value in original.items() if key != BUDGET_KEY}))

    def test_exact_owned_replay_keeps_first_cap_when_config_changes_or_turns_off(self):
        first = self.reserve(self.payload())
        before = ContentFactoryRun.objects.values().get(pk=first.pk)
        for usd, tokens in (("10.00", 100000), ("0", 0)):
            for include_saved in (False, True):
                payload = self.payload(**({BUDGET_KEY: deepcopy(BUDGET)} if include_saved else {}))
                with self.subTest(usd=usd, include_saved=include_saved), override_settings(
                        ARTICLES_RUN_CONTROL_MODEL_USD=usd, ARTICLES_RUN_CONTROL_MODEL_TOKENS=tokens):
                    self.assertEqual(self.reserve(payload).pk, first.pk)
                    self.assertEqual(payload[BUDGET_KEY], BUDGET)
                    self.assertEqual(ContentFactoryRun.objects.values().get(pk=first.pk), before)

    def test_fresh_external_budget_cannot_enroll_even_when_equal_to_backend_config(self):
        for value in (BUDGET, None, {**BUDGET, "max_model_usd": "999.00"}, True):
            payload = self.payload(**{BUDGET_KEY: value})
            before = deepcopy(payload)
            with self.subTest(value=value), self.assertRaises(RunOwnershipConflict):
                self.reserve(payload)
            self.assertEqual(payload, before)
        self.assertEqual(ContentFactoryRun.objects.count(), 0)

    def test_owned_replay_cannot_replace_first_budget_or_add_one_to_unbudgeted_admission(self):
        run = self.reserve(self.payload())
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        for value in ({**BUDGET, "max_model_usd": "6.00"}, {**BUDGET, "max_model_tokens": True}, None):
            with self.subTest(value=value), self.assertRaises(RunOwnershipConflict):
                self.reserve(self.payload(**{BUDGET_KEY: value}))
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)
        with override_settings(ARTICLES_RUN_CONTROL_MODEL_USD="0", ARTICLES_RUN_CONTROL_MODEL_TOKENS=0):
            original = self.reserve(self.payload(client_request_id="unbudgeted-original"))
        with self.assertRaises(RunOwnershipConflict):
            self.reserve(self.payload(client_request_id="unbudgeted-original", **{BUDGET_KEY: BUDGET}))
        replay = self.payload(client_request_id="unbudgeted-original")
        self.assertEqual(self.reserve(replay).pk, original.pk)
        self.assertNotIn(BUDGET_KEY, replay)

    def test_named_seal_adds_server_cap_to_complete_carried_charge_inputs_without_clock_changes(self):
        from django.contrib.auth import get_user_model
        from roo.models import Ledger
        user = get_user_model().objects.create_user(email="budget-seal@example.test")
        ledger = Ledger.objects.create(user=user, kind="SPEND", source="CONTENT_FACTORY", delta=-6,
            delta_microroo=-6_000_000, idempotency_key="synthetic-budget-original-spend")
        charge = Ledger.objects.values().get(pk=ledger.pk)
        request = self.payload(roo_points_ledger_id=ledger.pk, approved_body_sha256="b" * 64)
        with transaction.atomic():
            run = ContentFactoryRun.objects.create(run_id="budget-original", organization=self.org,
                domain=self.org.domain, workflow="article_generation", run_request=request,
                result={"points_v1": {"state": "carried"}})
            before = ContentFactoryRun.objects.values().get(pk=run.pk)
            seal_new_run_admission(run, created=True, principal=self.principal)
        run.refresh_from_db()
        self.assertEqual(run.run_request[BUDGET_KEY], BUDGET)
        self.assertEqual(owner_receipt(run)["identity"]["request_digest"], evidence_digest({**request, BUDGET_KEY: BUDGET}))
        self.assertEqual((run.created_at, run.updated_at), (before["created_at"], before["updated_at"]))
        self.assertEqual(Ledger.objects.values().get(pk=ledger.pk), charge)
        self.assertEqual(run.result["points_v1"], {"state": "carried"})

    def test_forged_budget_in_named_creation_rolls_back_before_any_dispatch(self):
        with self.assertRaises(RunOwnershipConflict), transaction.atomic():
            run = ContentFactoryRun.objects.create(run_id="budget-original", organization=self.org,
                domain=self.org.domain, workflow="article_generation", run_request=self.payload(**{BUDGET_KEY: BUDGET}))
            seal_new_run_admission(run, created=True, principal=self.principal)
        self.assertFalse(ContentFactoryRun.objects.exists())

    def test_sdk_budget_requires_independent_backend_optin_and_never_upgrades_existing_run(self):
        first = self.reserve(self.payload(), workflow="article_system_setup")
        self.assertNotIn(BUDGET_KEY, first.run_request)
        with override_settings(ARTICLES_RUN_SDK_CONTROL_ENABLED=True):
            replay = self.payload()
            self.assertEqual(self.reserve(replay, workflow="article_system_setup").pk, first.pk)
            self.assertNotIn(BUDGET_KEY, replay)
            fresh = self.reserve(self.payload(client_request_id="sdk-opted-in"), workflow="article_system_setup")
        self.assertEqual(fresh.run_request[BUDGET_KEY], {**BUDGET, "runtime": "maintained_sdk"})

    def test_public_and_worker_merge_cannot_expose_replace_or_forge_budget(self):
        from .service_views import _merge_django_owned_run_request
        run = self.reserve(self.payload())
        self.assertNotIn(BUDGET_KEY, public_run_value(run.run_request))
        self.assertEqual(_merge_django_owned_run_request(run.run_request,
            {BUDGET_KEY: {**BUDGET, "max_model_usd": "999"}})[BUDGET_KEY], BUDGET)
        self.assertNotIn(BUDGET_KEY, _merge_django_owned_run_request({}, {BUDGET_KEY: BUDGET}))
