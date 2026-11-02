"""Real transaction/lease proofs for admitted all-workflow history pulls."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import json
from threading import Event
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from . import run_reconciliation as reconcile
from .run_event_authority import receive_authoritative_run_events
from .run_ownership import (
    DISPATCH_RESULT_KEY, EVENT_RESULT_KEY, RECONCILIATION_RESULT_KEY, bind_admitted_run,
    current_execution_ticket, reserve_admitted_run, user_principal,
)
from .tests_run_event_authority import FLAGS, advance_ticket, batch


def page(run, *batches, cursor=None, more=False):
    seqs = [row["seq"] for body in batches for row in body["events"]]
    return {"schema_version": 1, "run_id": run.run_id, "batches": list(batches),
            "next_cursor": max(seqs) if cursor is None and seqs else cursor or 0, "has_more": more}


def response(body, status=200):
    raw = json.dumps(body).encode()
    return Mock(status_code=status, iter_content=Mock(return_value=[raw]))


@override_settings(**FLAGS, CONTENT_FACTORY_URL="https://worker.fixture.test", CONTENT_FACTORY_API_KEY="synthetic-service-key")
class AuthoritativeReconciliationTests(TransactionTestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic pull owner", domain="pull.example")
        self.now = timezone.now()

    def admit(self, key="pull-key", *, workflow="article_generation", bound=True):
        raw = {"domain": self.org.domain, "client_request_id": key, "topic": "Retained article body"}
        run = reserve_admitted_run(organization=self.org, principal=user_principal(123, "user:123"),
                                  workflow=workflow, payload=raw)
        return bind_admitted_run(run, f"worker-{key}"[:100]) if bound else run

    def sweep(self, now=None, limit=10):
        return reconcile.run_authoritative_reconciliation_sweep(now=now or self.now, limit=limit)

    def test_every_canonical_nonterminal_status_and_arbitrary_workflow_is_covered(self):
        runs = []
        for index, status in enumerate(sorted(reconcile.NONTERMINAL_STATUSES)):
            run = self.admit(f"nonterminal-{index}", workflow=f"future_workflow_{index}")
            ContentFactoryRun.objects.filter(pk=run.pk).update(status=status)
            runs.append(run)
        bodies = {run.run_id: page(run, batch(run, [{"status": "running"}])) for run in runs}

        def get(url, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(kwargs["headers"]["X-API-KEY"], "synthetic-service-key")
            self.assertEqual(kwargs["params"], {"after_seq": 0, "limit": 100})
            self.assertFalse(kwargs["allow_redirects"])
            return response(bodies[url.split("/")[-2]])

        with patch.object(reconcile.requests, "get", side_effect=get) as get:
            result = self.sweep(limit=100)
        self.assertEqual((result["checked"], result["history_received"], get.call_count), (len(runs),) * 3)
        self.assertTrue({"awaiting_approval", "awaiting_confirmation", "awaiting_delivery_mode",
                         "approval_required"} <= reconcile.NONTERMINAL_STATUSES)
        self.assertTrue(all(ContentFactoryRun.objects.get(pk=run.pk).status == "running" for run in runs))

    def test_terminal_rows_legacy_254_and_reservation_only_stay_untouched_with_flags_off(self):
        for status in sorted(reconcile.TERMINAL):
            run = self.admit(f"terminal-{status}")
            # A completed current projection is quiescent. A terminal label
            # without its current ticket's history still needs reconciliation.
            self.assertEqual(receive_authoritative_run_events(batch(run, [{"status": status}])).status_code, 200)
        with override_settings(ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False):
            self.admit("reservation-only")
        ContentFactoryRun.objects.bulk_create([ContentFactoryRun(run_id=f"historical-{n}",
            workflow="article_generation", domain=self.org.domain, organization=self.org, status="running") for n in range(254)])
        before = list(ContentFactoryRun.objects.order_by("pk").values())
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=False,
                               ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False), patch.object(reconcile.requests, "get") as get:
            self.assertEqual(self.sweep()["checked"], 0)
        get.assert_not_called()
        self.assertEqual(list(ContentFactoryRun.objects.order_by("pk").values()), before)

    def test_new_backend_ticket_recovers_lost_callback_despite_prior_terminal_status_and_flags_off(self):
        from .run_execution_control import append_execution_ticket
        run = self.admit("resumed-without-callback")
        old = batch(run, [{"status": "failed"}])
        with patch.object(reconcile.requests, "get", return_value=response(page(run, old))):
            self.assertEqual(self.sweep()["history_received"], 1)
        run.refresh_from_db()
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with transaction.atomic():
            locked = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
            ticket = append_execution_ticket(locked, control_key="f" * 64, reason="user_resume")
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.result[RECONCILIATION_RESULT_KEY]["cursor"], 1)
        fresh = batch(run, [{"status": "running", "seq": 2, "state_version": 2}], ticket)
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=False,
                               ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False), \
                patch.object(reconcile.requests, "get", return_value=response(page(run, fresh))) as get:
            self.assertEqual(self.sweep(now=self.now + timedelta(minutes=3))["history_received"], 1)
        self.assertEqual(get.call_args.kwargs["params"], {"after_seq": 1, "limit": 100})
        run.refresh_from_db()
        self.assertEqual(run.status, "running")
        self.assertEqual(run.result[RECONCILIATION_RESULT_KEY]["cursor"], 2)
        self.assertEqual(len(run.result[EVENT_RESULT_KEY]["events"]), 2)
        for field in ("run_request", "created_at", "updated_at", "last_event_emitted_at", "reconciled_at"):
            self.assertEqual(getattr(run, field), before[field], field)

    def test_missing_projection_is_pulled_without_inventing_status_or_compacting_saved_history(self):
        run = self.admit("missing-derived-projection")
        terminal = batch(run, [{"status": "failed"}])
        self.assertEqual(receive_authoritative_run_events(terminal).status_code, 200)
        run.refresh_from_db()
        result = deepcopy(run.result)
        result[EVENT_RESULT_KEY].pop("projection")
        ContentFactoryRun.objects.filter(pk=run.pk).update(result=result)
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False), \
                patch.object(reconcile.requests, "get", return_value=response(page(run, terminal))) as get:
            self.assertEqual(self.sweep()["history_received"], 1)
        self.assertEqual(get.call_args.kwargs["params"], {"after_seq": 0, "limit": 100})
        run.refresh_from_db()
        self.assertEqual(run.result[EVENT_RESULT_KEY]["events"], result[EVENT_RESULT_KEY]["events"])
        self.assertEqual(run.result[RECONCILIATION_RESULT_KEY]["cursor"], 1)
        for field in ("status", "run_request", "created_at", "updated_at", "last_event_emitted_at", "reconciled_at"):
            self.assertEqual(getattr(run, field), before[field], field)

    def test_existing_owner_continues_with_new_admission_flags_off_and_preserves_source_clocks(self):
        run = self.admit()
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=False,
                               ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False), patch.object(reconcile.requests, "get",
                return_value=response(page(run, batch(run, [{"status": "running"}])))):
            self.assertEqual(self.sweep()["history_received"], 1)
        after = ContentFactoryRun.objects.values().get(pk=run.pk)
        for field in before.keys() - {"result", "status"}:
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(after["result"][RECONCILIATION_RESULT_KEY]["cursor"], 1)
        self.assertNotIn("claim", after["result"][RECONCILIATION_RESULT_KEY])

    def test_retained_old_ticket_history_and_current_ticket_only_projection(self):
        run = self.admit()
        old = current_execution_ticket(run)
        current = advance_ticket(run, generation=0)
        first = batch(run, [{"seq": 1, "status": "completed"}], old)
        second = batch(run, [{"seq": 2, "state_version": 2, "status": "running"}], current)
        with patch.object(reconcile.requests, "get", return_value=response(page(run, first, second))):
            self.assertEqual(self.sweep()["history_received"], 1)
        run.refresh_from_db()
        self.assertEqual(run.status, "running")
        ledger = run.result[EVENT_RESULT_KEY]
        self.assertEqual(len(ledger["events"]), 2)
        self.assertEqual(ledger["projection"]["execution_id"], current["execution_id"])
        self.assertEqual(run.result[RECONCILIATION_RESULT_KEY]["cursor"], 2)

    def test_interleaved_tickets_keep_global_journal_order_instead_of_grouping_by_ticket(self):
        run = self.admit()
        old = current_execution_ticket(run)
        current = advance_ticket(run, generation=1)
        first = batch(run, [{"seq": 1, "status": "completed"}], old)
        second = batch(run, [{"seq": 2, "status": "running"}], current)
        third = batch(run, [{"seq": 3, "type": "cost", "usd": "0.01"}], old)
        with patch.object(reconcile.requests, "get", return_value=response(page(run, first, third, second))):
            self.assertEqual(self.sweep()["errors"], 1)
        run.refresh_from_db()
        self.assertNotIn(EVENT_RESULT_KEY, run.result)
        with patch.object(reconcile.requests, "get", return_value=response(page(run, first, second, third))):
            self.assertEqual(self.sweep(now=self.now + timedelta(seconds=31))["history_received"], 1)
        run.refresh_from_db()
        self.assertEqual(run.status, "running")
        self.assertEqual(len(run.result[EVENT_RESULT_KEY]["events"]), 3)

    def test_generation_changes_during_http_old_history_appends_without_projecting_or_losing_cursor(self):
        run = self.admit()
        old = current_execution_ticket(run)

        def get(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            run.refresh_from_db()  # The trusted control writer reads the locked current receipt.
            advance_ticket(run, generation=1)
            return response(page(run, batch(run, [{"status": "completed"}], old)))

        with patch.object(reconcile.requests, "get", side_effect=get):
            self.assertEqual(self.sweep()["history_received"], 1)
        run.refresh_from_db()
        self.assertEqual(run.status, "queued")
        self.assertEqual(len(run.result[EVENT_RESULT_KEY]["events"]), 1)
        self.assertEqual(run.result[RECONCILIATION_RESULT_KEY]["cursor"], 1)
        self.assertNotIn("last_projected_at", run.result[EVENT_RESULT_KEY])

    def test_generation_changes_during_key_lookup_cannot_bind_or_prove_no_effect(self):
        run = self.admit(bound=False)

        def lookup(claim):
            run.refresh_from_db()  # Preserve the in-flight claim as the real locked ticket service does.
            advance_ticket(run, generation=1)
            return "started", {"client_request_id": "pull-key", "run_id": "stale-worker"}

        with patch.object(reconcile, "_lookup", side_effect=lookup):
            self.assertEqual(self.sweep()["errors"], 1)
        run.refresh_from_db()
        self.assertEqual(run.run_id, "pull-key")
        self.assertIsNone(reconcile.no_effect_dispatch_proof(run))

    def test_fresh_pages_resume_persisted_cursor_and_keep_reserved_gaps(self):
        run = self.admit()
        one = batch(run, [{"seq": 1, "status": "running"}])
        three = batch(run, [{"seq": 3, "state_version": 3, "type": "step_started", "step": "write", "attempt": 1}])
        with patch.object(reconcile.requests, "get", side_effect=[response(page(run, one, more=True)),
                response(page(run, three))]) as get:
            self.assertEqual(self.sweep()["history_received"], 1)
            self.assertEqual(self.sweep()["history_received"], 1)
        self.assertEqual([call.kwargs["params"]["after_seq"] for call in get.call_args_list], [0, 1])
        run.refresh_from_db()
        self.assertEqual(run.result[RECONCILIATION_RESULT_KEY]["cursor"], 3)
        self.assertEqual(sorted(row["event"]["seq"] for row in run.result[EVENT_RESULT_KEY]["events"].values()), [1, 3])

    def test_exact_callback_replay_advances_pull_cursor_without_touching_projection_clock(self):
        run = self.admit()
        raw = batch(run, [{"status": "running"}])
        self.assertEqual(receive_authoritative_run_events(raw).status_code, 200)
        run.refresh_from_db()
        ledger = deepcopy(run.result[EVENT_RESULT_KEY])
        with patch.object(reconcile.requests, "get", return_value=response(page(run, raw))):
            self.assertEqual(self.sweep()["history_received"], 1)
        run.refresh_from_db()
        self.assertEqual(run.result[EVENT_RESULT_KEY], ledger)
        self.assertEqual(run.result[RECONCILIATION_RESULT_KEY]["cursor"], 1)

    def test_terminal_first_page_still_drains_announced_immutable_history(self):
        run = self.admit()
        terminal = batch(run, [{"status": "completed"}])
        cost = batch(run, [{"seq": 2, "state_version": 2, "type": "cost", "calls": 1, "usd": "0.01"}])
        with patch.object(reconcile.requests, "get", side_effect=[response(page(run, terminal, more=True)),
                response(page(run, cost))]) as get:
            self.assertEqual(self.sweep()["history_received"], 1)
            self.assertEqual(self.sweep()["history_received"], 1)
            self.assertEqual(self.sweep(now=self.now + timedelta(hours=1))["checked"], 0)
        self.assertEqual(get.call_count, 2)
        run.refresh_from_db()
        self.assertEqual(run.status, "completed")
        self.assertEqual(len(run.result[EVENT_RESULT_KEY]["events"]), 2)
        self.assertEqual(run.result[RECONCILIATION_RESULT_KEY]["cursor"], 2)

    def test_continuous_has_more_cannot_exceed_one_page_per_run_per_tick(self):
        run = self.admit()
        remote = response(page(run, batch(run, [{"status": "running"}]), more=True))
        with patch.object(reconcile.requests, "get", return_value=remote) as get:
            result = self.sweep(limit=1)
        self.assertEqual((result["checked"], result["history_received"], get.call_count), (1, 1, 1))
        run.refresh_from_db()
        self.assertTrue(run.result[RECONCILIATION_RESULT_KEY]["has_more"])

    def test_later_conflict_rolls_back_entire_page_and_keeps_cursor_for_exact_retry(self):
        run = self.admit()
        existing = batch(run, [{"seq": 2, "state_version": 2, "status": "running"}])
        self.assertEqual(receive_authoritative_run_events(existing).status_code, 200)
        first = batch(run, [{"seq": 1, "type": "step_started", "step": "write", "attempt": 1}])
        conflict = batch(run, [{"seq": 2, "state_version": 2, "status": "completed"}])
        with patch.object(reconcile.requests, "get", return_value=response(page(run, first, conflict))):
            self.assertEqual(self.sweep()["errors"], 1)
        run.refresh_from_db()
        self.assertEqual(len(run.result[EVENT_RESULT_KEY]["events"]), 1)
        self.assertFalse(run.steps.exists())
        self.assertEqual(run.result[RECONCILIATION_RESULT_KEY].get("cursor", 0), 0)
        with patch.object(reconcile.requests, "get", return_value=response(page(run, first, existing))):
            self.assertEqual(self.sweep(now=self.now + timedelta(seconds=31))["history_received"], 1)
        run.refresh_from_db()
        self.assertEqual(len(run.result[EVENT_RESULT_KEY]["events"]), 2)

    def test_scope_digest_mismatch_cannot_append_or_advance_cursor(self):
        from .run_event_authority import event_digest
        run = self.admit()
        raw = batch(run, [{"status": "completed"}])
        raw["backend_run_execution_v1"]["scope"]["source_run_id"] = "foreign-run"
        for row in raw["events"]:
            row["digest"] = event_digest(run.run_id, raw["backend_run_admission_v1"], raw["backend_run_execution_v1"], row)
        with patch.object(reconcile.requests, "get", return_value=response(page(run, raw))):
            self.assertEqual(self.sweep()["errors"], 1)
        run.refresh_from_db()
        self.assertNotIn(EVENT_RESULT_KEY, run.result)
        self.assertEqual(run.status, "queued")

    def test_nonce_replacement_fences_a_late_history_response(self):
        run = self.admit()
        old = reconcile._claim(run.pk, self.now)
        new = reconcile._claim(run.pk, self.now + timedelta(seconds=61))
        self.assertNotEqual(old["nonce"], new["nonce"])
        raw = page(run, batch(run, [{"status": "completed"}]))
        self.assertEqual(reconcile._apply_page(old, raw, self.now), "superseded")
        run.refresh_from_db()
        self.assertNotIn(EVENT_RESULT_KEY, run.result)
        self.assertEqual(reconcile._apply_page(new, raw, self.now + timedelta(seconds=61)), "history_received")

    def test_cancel_during_http_preserves_cancel_and_only_appends_owned_history(self):
        run = self.admit()
        claim = reconcile._claim(run.pk, self.now)
        ContentFactoryRun.objects.filter(pk=run.pk).update(status="cancelled")
        self.assertEqual(reconcile._apply_page(claim, page(run, batch(run, [{"status": "running"}])), self.now), "history_received")
        run.refresh_from_db()
        self.assertEqual(run.status, "cancelled")
        self.assertEqual(len(run.result[EVENT_RESULT_KEY]["events"]), 1)

    def test_failed_oldest_run_does_not_starve_other_runs_at_bounded_limit(self):
        runs = [self.admit(f"fair-{n}") for n in range(5)]
        seen = []

        def get(url, **kwargs):
            identifier = url.split("/")[-2]
            seen.append(identifier)
            if identifier == runs[0].run_id:
                raise reconcile.requests.Timeout("synthetic timeout, never no-effect proof")
            run = next(run for run in runs if run.run_id == identifier)
            return response(page(run, cursor=0))

        with patch.object(reconcile.requests, "get", side_effect=get):
            for _ in range(5):
                self.assertEqual(self.sweep(limit=1)["checked"], 1)
        self.assertEqual(seen, [run.run_id for run in runs])
        self.assertEqual(self.sweep(limit=1)["checked"], 0)

    def test_damaged_owner_is_throttled_without_losing_cursor_or_starving_valid_owner(self):
        from .run_ownership import OWNER_EXECUTION_KEY
        damaged, valid = self.admit("damaged"), self.admit("healthy")
        result = deepcopy(damaged.result)
        result.pop(OWNER_EXECUTION_KEY)
        result[RECONCILIATION_RESULT_KEY] = {"cursor": 7}
        ContentFactoryRun.objects.filter(pk=damaged.pk).update(result=result)
        with patch.object(reconcile.requests, "get", return_value=response(page(valid, cursor=0))) as get:
            self.assertEqual(self.sweep(limit=1)["errors"], 1)
            get.assert_not_called()
            self.assertEqual(self.sweep(limit=1)["history_received"], 1)
        damaged.refresh_from_db()
        self.assertEqual(damaged.result[RECONCILIATION_RESULT_KEY]["cursor"], 7)

    def test_no_http_is_permitted_inside_any_database_transaction(self):
        run = self.admit()
        claim = reconcile._claim(run.pk, self.now)
        with patch.object(reconcile.requests, "get") as get, transaction.atomic():
            with self.assertRaisesRegex(reconcile.RunReconciliationError, "http_under_database_lock"):
                reconcile._fetch_page("https://fixture.test", {}, claim)
        get.assert_not_called()

    def test_corrupt_metadata_is_retained_and_does_not_starve_later_lost_callback_ticket(self):
        from .run_execution_control import append_execution_ticket
        from .run_ownership import HISTORY_TRANSPORT_RESULT_KEY, public_run_value
        damaged, resumed = self.admit("opaque-history"), self.admit("later-ticket")
        old = batch(resumed, [{"status": "failed"}])
        with patch.object(reconcile.requests, "get", return_value=response(page(resumed, old))):
            self.assertEqual(reconcile.reconcile_authoritative_run(resumed.run_id, now=self.now)["cursor"], 1)
        with transaction.atomic():
            locked = ContentFactoryRun.objects.select_for_update().get(pk=resumed.pk)
            ticket = append_execution_ticket(locked, control_key="e" * 64, reason="user_resume")
        resumed.refresh_from_db()
        fresh = batch(resumed, [{"seq": 2, "state_version": 2, "status": "running"}], ticket)
        corruptions = ["opaque history: do not reset", None, ["retained", 7], {"cursor": True}]
        for corrupt in corruptions:
            with self.subTest(corrupt=corrupt):
                damaged.refresh_from_db()
                result = deepcopy(damaged.result)
                result.pop(HISTORY_TRANSPORT_RESULT_KEY, None)
                result[RECONCILIATION_RESULT_KEY] = corrupt
                ContentFactoryRun.objects.filter(pk=damaged.pk).update(result=result)
                before = ContentFactoryRun.objects.values().get(pk=damaged.pk)
                with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=False,
                                       ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False), patch.object(reconcile.requests, "get") as get:
                    self.assertEqual(self.sweep(limit=1)["errors"], 1)
                    get.assert_not_called()
                damaged.refresh_from_db()
                self.assertEqual(damaged.result, {**before["result"],
                    HISTORY_TRANSPORT_RESULT_KEY: damaged.result[HISTORY_TRANSPORT_RESULT_KEY]})
                self.assertEqual(damaged.result[HISTORY_TRANSPORT_RESULT_KEY]["last_outcome"],
                    "admission_invalid" if isinstance(corrupt, dict) else "metadata_invalid")
                for field, value in before.items():
                    if field != "result":
                        self.assertEqual(getattr(damaged, field), value, field)
                self.assertNotIn(HISTORY_TRANSPORT_RESULT_KEY, public_run_value(damaged.result))
                self.assertIsNone(reconcile.no_effect_dispatch_proof(damaged))
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=False,
                               ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False), \
                patch.object(reconcile.requests, "get", return_value=response(page(resumed, fresh))) as get:
            self.assertEqual(self.sweep(now=self.now + timedelta(minutes=3), limit=1)["history_received"], 1)
        self.assertEqual(get.call_args.kwargs["params"]["after_seq"], 1)
        resumed.refresh_from_db()
        self.assertEqual(resumed.status, "running")
        self.assertEqual(resumed.result[RECONCILIATION_RESULT_KEY]["cursor"], 2)
        self.assertEqual(len(resumed.result[EVENT_RESULT_KEY]["events"]), 2)

    def test_metadata_corrupted_during_http_holds_without_repair_or_projection(self):
        from .run_ownership import HISTORY_TRANSPORT_RESULT_KEY
        run = self.admit("corrupt-during-io")
        before = ContentFactoryRun.objects.values().get(pk=run.pk)

        def get(*args, **kwargs):
            current = ContentFactoryRun.objects.get(pk=run.pk)
            ContentFactoryRun.objects.filter(pk=run.pk).update(result={**current.result,
                RECONCILIATION_RESULT_KEY: "opaque-after-claim"})
            return response(page(run, batch(run, [{"status": "completed"}])))

        with patch.object(reconcile.requests, "get", side_effect=get):
            result = reconcile.reconcile_authoritative_run(run.run_id, now=self.now, refresh=True)
        self.assertEqual((result["status"], result["outcome"], result["cursor"]), ("pending", "metadata_invalid", None))
        run.refresh_from_db()
        self.assertEqual(run.result[RECONCILIATION_RESULT_KEY], "opaque-after-claim")
        self.assertNotIn(EVENT_RESULT_KEY, run.result)
        self.assertEqual(run.result[HISTORY_TRANSPORT_RESULT_KEY]["last_outcome"], "metadata_invalid")
        for field, value in before.items():
            if field != "result":
                self.assertEqual(getattr(run, field), value, field)

    def test_one_run_control_refresh_bypasses_only_cadence_and_returns_drained_cursor(self):
        run = self.admit()
        first = page(run, batch(run, [{"status": "running"}]))
        with patch.object(reconcile.requests, "get", return_value=response(first)):
            result = reconcile.reconcile_authoritative_run(run.run_id, now=self.now)
        self.assertEqual((result["status"], result["outcome"], result["cursor"]),
                         ("completed", "history_received", 1))
        with patch.object(reconcile.requests, "get") as get:
            result = reconcile.reconcile_authoritative_run(run.run_id, now=self.now)
        self.assertEqual(result["outcome"], "not_due")
        get.assert_not_called()
        with patch.object(reconcile.requests, "get", return_value=response(page(run, cursor=1))) as get:
            result = reconcile.reconcile_authoritative_run(run.run_id, now=self.now, refresh=True)
        self.assertEqual((result["status"], result["cursor"], result["has_more"], result["active_claim"]),
                         ("completed", 1, False, False))
        self.assertEqual(get.call_args.kwargs["params"]["after_seq"], 1)

    def test_control_refresh_never_steals_an_active_claim_or_reads_under_a_lock(self):
        run = self.admit()
        old = reconcile._claim(run.pk, self.now)
        with patch.object(reconcile.requests, "get") as get:
            result = reconcile.reconcile_authoritative_run(run.run_id, now=self.now, refresh=True)
            self.assertEqual((result["status"], result["outcome"], result["active_claim"]), ("pending", "not_due", True))
            with transaction.atomic(), self.assertRaisesRegex(reconcile.RunReconciliationError, "http_under_database_lock"):
                reconcile.reconcile_authoritative_run(run.run_id, now=self.now, refresh=True)
        get.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.result[RECONCILIATION_RESULT_KEY]["claim"]["nonce"], old["nonce"])

    def test_terminal_control_refresh_only_observes_retained_failure_never_reopens_it(self):
        run = self.admit()
        raw = batch(run, [{"status": "failed"}])
        self.assertEqual(receive_authoritative_run_events(raw).status_code, 200)
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        with patch.object(reconcile.requests, "get", return_value=response(page(run, raw))):
            result = reconcile.reconcile_authoritative_run(run.run_id, now=self.now, refresh=True)
        self.assertEqual((result["outcome"], result["status"], result["cursor"]), ("history_received", "completed", 1))
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")

    def test_control_refresh_more_history_returns_pending_until_an_exact_next_page_drains(self):
        run = self.admit()
        with patch.object(reconcile.requests, "get", side_effect=[
                response(page(run, batch(run, [{"status": "running"}]), more=True)),
                response(page(run, batch(run, [{"seq": 2, "state_version": 2, "type": "cost", "usd": "0.01"}])))]):
            first = reconcile.reconcile_authoritative_run(run.run_id, now=self.now, refresh=True)
            second = reconcile.reconcile_authoritative_run(run.run_id, now=self.now, refresh=True)
        self.assertEqual((first["status"], first["has_more"], first["cursor"]), ("pending", True, 1))
        self.assertEqual((second["status"], second["has_more"], second["cursor"]), ("completed", False, 2))

    def test_sweep_wall_time_budget_leaves_unclaimed_work_for_the_next_tick(self):
        run = self.admit()
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with patch.object(reconcile, "monotonic", side_effect=[0, 46]), patch.object(reconcile.requests, "get") as get:
            self.assertEqual(self.sweep()["checked"], 0)
        get.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)

    def test_unknown_ack_binds_exact_original_long_key_without_new_dispatch_or_clock_change(self):
        key = "logical-" + "x" * 247
        run = self.admit(key, bound=False)
        from .dispatch_admission import pending_response
        pending_response(run)
        run.refresh_from_db()
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with patch.object(reconcile, "_lookup", return_value=("started", {
                "client_request_id": key, "run_id": "actual-worker-id"})) as lookup, \
                patch.object(reconcile.requests, "post") as post:
            self.assertEqual(self.sweep()["dispatch_bound"], 1)
        self.assertEqual(lookup.call_args.args[0]["dispatch_key"], key)
        post.assert_not_called()
        after = ContentFactoryRun.objects.values().get(pk=run.pk)
        self.assertEqual(after["run_id"], "actual-worker-id")
        self.assertEqual((after["status"], after["current_step"]), ("queued", "queued"))
        for field in before.keys() - {"run_id", "result", "status", "current_step"}:
            self.assertEqual(after[field], before[field], field)
        run.refresh_from_db()
        with patch.object(reconcile.requests, "get", return_value=response(page(run, batch(run, [{"status": "running"}])))):
            self.assertEqual(self.sweep(now=self.now + timedelta(seconds=121))["history_received"], 1)
        run.refresh_from_db()
        self.assertEqual(run.status, "running")

    def test_process_loss_after_reservation_before_post_still_uses_original_key(self):
        run = self.admit(bound=False)
        self.assertNotIn(DISPATCH_RESULT_KEY, run.result)
        with patch.object(reconcile, "_lookup", return_value=("started", {
                "client_request_id": "pull-key", "run_id": "accepted-before-process-loss"})) as lookup, \
                patch.object(reconcile.requests, "get") as get, patch.object(reconcile.requests, "post") as post:
            self.assertEqual(self.sweep()["dispatch_bound"], 1)
        self.assertEqual(lookup.call_args.args[0]["dispatch_key"], "pull-key")
        get.assert_not_called()
        post.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.run_id, "accepted-before-process-loss")

    def test_current_callback_first_can_resolve_only_backend_private_transport_hold(self):
        from .dispatch_admission import pending_response
        run = self.admit(bound=False)
        pending_response(run)
        run.refresh_from_db()
        run = bind_admitted_run(run, "callback-first-worker")
        self.assertEqual((run.status, run.current_step), ("queued", "queued"))
        raw = batch(run, [{"status": "running", "step": "write"}])
        self.assertEqual(receive_authoritative_run_events(raw).status_code, 200)
        run.refresh_from_db()
        self.assertEqual((run.status, run.current_step), ("running", "write"))
        # An actual backend stop, using another step, remains authoritative.
        ContentFactoryRun.objects.filter(pk=run.pk).update(status="blocked", current_step="owner_stopped")
        later = batch(run, [{"seq": 2, "state_version": 2, "status": "running", "step": "write"}])
        self.assertEqual(receive_authoritative_run_events(later).status_code, 200)
        run.refresh_from_db()
        self.assertEqual((run.status, run.current_step), ("blocked", "owner_stopped"))

    def test_no_effect_requires_fresh_exact_key_proof_after_grace_and_never_settles(self):
        from roo.models import Ledger
        from .dispatch_admission import pending_response
        run = self.admit(bound=False)
        pending_response(run)
        run.refresh_from_db()
        now = run.created_at + timedelta(seconds=1)
        original = ContentFactoryRun.objects.values().get(pk=run.pk)
        money = Ledger.objects.count()
        with patch.object(reconcile, "_lookup", return_value=("not_received", {})):
            self.assertEqual(self.sweep(now=now)["dispatch_pending"], 1)
        run.refresh_from_db()
        self.assertIsNone(reconcile.no_effect_dispatch_proof(run))
        later = run.created_at + timedelta(seconds=181)
        with patch.object(reconcile, "_lookup", return_value=("not_received", {})):
            self.assertEqual(self.sweep(now=later)["dispatch_no_effect_proven"], 1)
        run.refresh_from_db()
        self.assertEqual(reconcile.no_effect_dispatch_proof(run)["dispatch_key"], "pull-key")
        proof_history = deepcopy(run.result[RECONCILIATION_RESULT_KEY]["no_effect_proofs"])
        self.assertEqual(Ledger.objects.count(), money)
        current = ContentFactoryRun.objects.values().get(pk=run.pk)
        for field in original.keys() - {"result"}:
            self.assertEqual(current[field], original[field], field)
        with patch.object(reconcile, "_lookup", return_value=("unknown", {"error": "timeout"})):
            self.assertEqual(self.sweep(now=later + timedelta(seconds=121))["dispatch_pending"], 1)
        run.refresh_from_db()
        self.assertIsNone(reconcile.no_effect_dispatch_proof(run))
        self.assertEqual(run.result[RECONCILIATION_RESULT_KEY]["no_effect_proofs"], proof_history)

    def test_no_effect_handoff_cannot_cross_a_new_same_generation_source_ticket(self):
        from .dispatch_admission import pending_response
        run = self.admit(bound=False)
        pending_response(run)
        self.now = run.created_at + timedelta(seconds=181)
        with patch.object(reconcile, "_lookup", return_value=("not_received", {})):
            self.assertEqual(self.sweep()["dispatch_no_effect_proven"], 1)
        run.refresh_from_db()
        proof_history = deepcopy(run.result[RECONCILIATION_RESULT_KEY]["no_effect_proofs"])
        self.assertIsNotNone(reconcile.no_effect_dispatch_proof(run))
        advance_ticket(run, generation=0)
        self.assertIsNone(reconcile.no_effect_dispatch_proof(run))
        self.assertEqual(run.result[RECONCILIATION_RESULT_KEY]["no_effect_proofs"], proof_history)

    def test_lookup_changed_key_and_occupied_worker_identity_retain_original_slot(self):
        from .dispatch_admission import pending_response
        run = self.admit(bound=False)
        pending_response(run)
        ContentFactoryRun.objects.create(run_id="occupied", workflow="legacy", domain="elsewhere.test")
        for body in ({"client_request_id": "foreign", "run_id": "foreign-worker"},
                     {"client_request_id": "pull-key", "run_id": "occupied"}):
            with patch.object(reconcile, "_lookup", return_value=("started", body)):
                self.assertEqual(self.sweep()["errors"], 1)
            run.refresh_from_db()
            self.assertEqual(run.run_id, "pull-key")
            self.now += timedelta(hours=2)
        self.assertEqual(ContentFactoryRun.objects.count(), 2)

    def test_enabled_legacy_sweep_and_direct_effect_helpers_cannot_compete(self):
        from . import reconciliation as legacy
        run = self.admit()
        ContentFactoryRun.objects.filter(pk=run.pk).update(updated_at=self.now - timedelta(days=1))
        run.refresh_from_db()
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with patch.object(legacy.requests, "get") as get, patch.object(legacy.requests, "post") as post:
            self.assertEqual(legacy.run_content_factory_reconciliation_sweep(now=self.now)["checked"], 0)
            self.assertEqual(legacy.recover_stalled_run(run, base_url="https://fixture.test", headers={}, now=self.now), "authoritative_owned")
            self.assertEqual(legacy._stop_stalled_article(run, base_url="https://fixture.test", headers={}, now=self.now), "authoritative_owned")
            self.assertEqual(legacy._adopt_remote_payload(run, {"status": "completed"}), "queued")
            self.assertFalse(legacy._finalize_run_failure(run.run_id, error="synthetic", outcome="synthetic", now=self.now))
            legacy._stamp_reconciled(run.run_id, self.now)
            legacy._refund_failed_run(run, reason="synthetic")
        get.assert_not_called()
        post.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)

    def test_legacy_terminal_refund_redrive_and_restart_candidates_exclude_event_owners(self):
        from . import reconciliation as legacy
        for status in sorted(reconcile.TERMINAL):
            run = self.admit(f"legacy-fence-{status}")
            ContentFactoryRun.objects.filter(pk=run.pk).update(status=status, updated_at=self.now - timedelta(days=1),
                result={**run.result, "reconciliation_refund_pending": True,
                    "error_code": "CONTENT_FACTORY_DISPATCH_REJECTED", "restart_receipt": {"state": "pending", "client_request_id": "retained"}})
        before = list(ContentFactoryRun.objects.order_by("pk").values())
        with patch.object(legacy.requests, "get") as get, patch.object(legacy.requests, "post") as post:
            self.assertEqual(legacy.run_content_factory_reconciliation_sweep(now=self.now)["checked"], 0)
        get.assert_not_called()
        post.assert_not_called()
        self.assertEqual(list(ContentFactoryRun.objects.order_by("pk").values()), before)

    def test_legacy_orphan_operation_lookup_and_refund_cannot_bypass_original_key_owner(self):
        from . import reconciliation as legacy
        from .website_models import WebsiteConnection, WebsiteConnectionOperation
        run = self.admit(bound=False)
        website = WebsiteConnection.objects.create(organization=self.org, github_repo="synthetic/site", generation=1)
        op = WebsiteConnectionOperation.objects.create(connection=website, generation=1,
            idempotency_key="owned-orphan", action="workflow", state="failed",
            payload={"client_request_id": "pull-key"}, receipt={"refund_pending": True})
        before = WebsiteConnectionOperation.objects.values().get(pk=op.pk)
        with patch("content_factory.vibe_marketing_views._lookup_content_factory_dispatch_by_key") as lookup, \
                patch("integrations.services.article_generation.refund_content_factory_request_for_user") as refund:
            self.assertEqual(legacy._lookup_orphan_child(op), ("unknown", {}))
            legacy._refund_orphan_operation(op)
            with self.assertRaisesRegex(ValueError, "admitted event owner"):
                legacy._adopt_orphan_child(op, {"run_id": "foreign-worker"})
        lookup.assert_not_called()
        refund.assert_not_called()
        self.assertEqual(WebsiteConnectionOperation.objects.values().get(pk=op.pk), before)
        run.refresh_from_db()
        self.assertEqual(run.run_id, "pull-key")

    def test_actual_scheduler_registers_unconditional_owner_runner_before_legacy(self):
        from core.management.commands import run_scheduled_discovery as command
        from io import StringIO
        runner = command.Command(stdout=StringIO())
        with patch.object(command, "run_runners", return_value=({}, [])) as orchestrate:
            runner.handle()
        rows = orchestrate.call_args.args[0]
        names = [name for name, _ in rows]
        self.assertLess(names.index("authoritative_run_reconciliation"), names.index("content_factory_reconciliation"))
        self.assertIs(dict(rows)["authoritative_run_reconciliation"], reconcile.run_authoritative_reconciliation_sweep)

    @skipUnless(connection.vendor == "postgresql", "Actual row locking requires disposable PostgreSQL")
    def test_concurrent_sweeps_admit_one_http_pull_under_the_durable_nonce(self):
        run = self.admit()
        entered, release = Event(), Event()

        def get(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            entered.set()
            if not release.wait(10):
                raise AssertionError("Synthetic peer did not release its held response")
            return response(page(run, batch(run, [{"status": "running"}])))

        def sweep():
            close_old_connections()
            try:
                return self.sweep(limit=1)
            finally:
                close_old_connections()

        with patch.object(reconcile.requests, "get", side_effect=get) as get, ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(sweep)
            self.assertTrue(entered.wait(10))
            second = pool.submit(sweep)
            try:
                self.assertEqual(second.result(timeout=10)["checked"], 0)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=10)["history_received"], 1)
        self.assertEqual(get.call_count, 1)
