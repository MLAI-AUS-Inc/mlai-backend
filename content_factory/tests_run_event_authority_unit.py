"""Pure protocol/reducer regressions; no database or network is used."""
from copy import deepcopy
from pathlib import Path
import hashlib
import json

from django.test import SimpleTestCase

from .run_event_authority import (
    EVENT_NAME, RunEventConflict, RunEventInvalid, canonical_bytes, event_digest,
    fold_authoritative_events, validate_authoritative_envelope,
)

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "docs/run-event-authority-v1.fixture.json").read_text())


def event(seq=1, event_type="run_state", **values):
    fields = {"seq": seq, "generation": 0, "state_version": seq, "type": event_type,
              "at": "2026-11-03T00:00:00+00:00"}
    if event_type == "run_state":
        fields["status"] = "running"
    fields.update(values)
    return fields


def payload(events=None, *, execution=None):
    batch = deepcopy(FIXTURE)
    if execution:
        batch["backend_run_execution_v1"].update(execution)
    batch["events"] = deepcopy(events if events is not None else [event()])
    for row in batch["events"]:
        row["digest"] = event_digest(batch["run_id"], batch["backend_run_admission_v1"],
                                     batch["backend_run_execution_v1"], row)
    return batch


class ProtocolTests(SimpleTestCase):
    def test_published_fixture_has_the_exact_canonical_digest(self):
        batch = validate_authoritative_envelope(FIXTURE)
        self.assertEqual(batch.run_id, "run-fixture-1")
        self.assertEqual(batch.events[0]["digest"], FIXTURE["events"][0]["digest"])
        self.assertEqual(EVENT_NAME, "run_events_v2")

    def test_unicode_material_uses_the_same_ascii_escape_form_as_backend_evidence_digest(self):
        from .website_contract import evidence_digest
        value = {"actor": "Zoë 树"}
        self.assertIn(b"\\u", canonical_bytes(value))
        self.assertEqual(hashlib.sha256(canonical_bytes(value)).hexdigest(), evidence_digest(value))

    def test_validation_copies_inputs_and_does_not_retain_mutable_caller_json(self):
        raw = payload()
        before = deepcopy(raw)
        batch = validate_authoritative_envelope(raw)
        raw["events"][0]["status"] = "completed"
        raw["backend_run_execution_v1"]["scope"]["source_run_id"] = "changed"
        self.assertEqual(batch.events[0]["event"]["status"], "running")
        self.assertEqual(batch.execution, before["backend_run_execution_v1"])

    def test_top_level_private_fields_and_old_protocol_are_not_admitted(self):
        for fields in ({"result": {}}, {"run_request": {}}, {"approval_state": "approved"},
                       {"event": "run_events"}, {"generation": 0}, {"state_version": 1}):
            with self.subTest(fields=fields), self.assertRaises(RunEventInvalid):
                validate_authoritative_envelope({**payload(), **fields})

    def test_typed_identifiers_versions_and_event_fields_fail_closed(self):
        for change in ({"seq": True}, {"seq": 0}, {"generation": "0"}, {"state_version": False},
                       {"type": []}, {"status": {}}, {"at": "2026-11-03"}, {"type": "publish"},
                       {"billing_amount": 9}, {"approved": True}, {"status": "processing"}):
            with self.subTest(change=change), self.assertRaises(RunEventInvalid):
                validate_authoritative_envelope(payload([{**event(), **change}]))
        for row in (event(event_type="step_started", step="../path", attempt=1),
                    event(event_type="step_done", step="write", attempt=False, status="completed"),
                    event(event_type="artifact", step="write", kind_value="secret", kind="json")):
            # Constructing a malformed artifact is handled separately below.
            if row["type"] == "artifact":
                row["path"] = "private/path"
            with self.subTest(row=row), self.assertRaises(RunEventInvalid):
                validate_authoritative_envelope(payload([row]))

    def test_digest_covers_original_full_admission_ticket_version_and_body(self):
        raw = payload()
        changes = [lambda x: x["events"][0].update(state_version=99),
                   lambda x: x["backend_run_admission_v1"].update(request_digest="b" * 64),
                   lambda x: x["backend_run_execution_v1"]["scope"].update(expected_source_sha="b" * 40),
                   lambda x: x.update(run_id="another-run")]
        for mutate in changes:
            changed = deepcopy(raw)
            mutate(changed)
            with self.assertRaises(RunEventInvalid):
                validate_authoritative_envelope(changed)

    def test_generation_must_match_its_retained_ticket_and_echo_types_are_exact(self):
        with self.assertRaises(RunEventInvalid):
            validate_authoritative_envelope(payload([event(generation=1)]))
        for field in ("schema_version", "event_protocol"):
            raw = payload()
            raw["backend_run_admission_v1"][field] = True
            with self.assertRaises(RunEventInvalid):
                validate_authoritative_envelope(raw)

    def test_failure_is_membership_based_and_version_hash_diagnostics_do_not_pin_runtime(self):
        from .website_failure_contract import load_registry
        known = "ARTICLE_DIRECTORY_AGENT_OUTPUT_INVALID"
        raw = payload([event(event_type="failure", failure={"code": known, "stage": "write",
            "registry_version": 999999, "registry_sha256": "b" * 64})])
        batch = validate_authoritative_envelope(raw)
        view = batch.events[0]["facts"]["failure"]
        self.assertEqual(view["code"], known)
        self.assertEqual(view["failure_class"], load_registry()["by_code"][known]["failure_class"])
        unknown = validate_authoritative_envelope(payload([event(event_type="failure", failure={
            "code": "FUTURE_WORKER_CAUSE", "stage": "write"})])).events[0]["facts"]["failure"]
        self.assertEqual(unknown["code"], "UNREGISTERED_FAILURE_CODE")
        self.assertNotIn("FUTURE_WORKER_CAUSE", unknown["user_message"])
        for field, value in (("failure_class", "transient"), ("retryable", True),
                             ("action", {"id": "approve"}), ("user_message", "raw"), ("findings", [])):
            with self.subTest(field=field), self.assertRaises(RunEventInvalid):
                validate_authoritative_envelope(payload([event(event_type="failure", failure={
                    "code": known, "stage": "write", field: value})]))

    def test_cost_and_artifact_are_typed_observations_not_arbitrary_payloads(self):
        rows = [event(1, "artifact", step="write", kind="json", content_sha256="a" * 64),
                event(2, "cost", step="write", calls=2, tokens=200, usd="0.1200")]
        self.assertEqual(len(validate_authoritative_envelope(payload(rows)).events), 2)
        for row in (event(event_type="cost", calls=True), event(event_type="cost", usd="NaN"),
                    event(event_type="cost", usd="-1"), event(event_type="cost", usd=1.0),
                    event(event_type="artifact", step="write", kind="html", url="https://private.test")):
            with self.subTest(row=row), self.assertRaises(RunEventInvalid):
                validate_authoritative_envelope(payload([row]))


class ReducerTests(SimpleTestCase):
    def fold(self, saved, events, *, execution=None, current="execution-fixture-1", **kwargs):
        batch = validate_authoritative_envelope(payload(events, execution=execution))
        return fold_authoritative_events(saved, batch, current_execution_id=current, **kwargs)

    def test_exact_replay_is_one_application_changed_same_identity_conflicts(self):
        ledger, accepted, duplicates = self.fold(None, [event()])
        self.assertEqual((accepted, duplicates), (1, 0))
        replay, accepted, duplicates = self.fold(ledger, [event()])
        self.assertEqual((accepted, duplicates), (0, 1))
        self.assertEqual(replay, ledger)
        before = deepcopy(ledger)
        with self.assertRaises(RunEventConflict):
            self.fold(ledger, [event(status="completed")])
        self.assertEqual(ledger, before)

    def test_more_than_64_gaps_never_turn_an_unseen_event_into_a_duplicate(self):
        ledger = None
        for start in (1, 201):
            ledger, accepted, _ = self.fold(ledger, [event(seq) for seq in range(start, start + 200, 2)])
            self.assertEqual(accepted, 100)
        ledger, accepted, duplicates = self.fold(ledger, [event(2)])
        self.assertEqual((len(ledger["events"]), accepted, duplicates), (201, 1, 0))

    def test_finished_attempt_cannot_return_to_running_and_new_attempt_can_start(self):
        ledger, _, _ = self.fold(None, [event(1, "step_done", step="write", attempt=1, status="completed")])
        ledger, _, _ = self.fold(ledger, [event(2, "step_started", step="write", attempt=1)])
        self.assertEqual(ledger["projection"]["steps"]["write"]["status"], "completed")
        ledger, _, _ = self.fold(ledger, [event(3, "step_started", step="write", attempt=2)])
        self.assertEqual(ledger["projection"]["steps"]["write"]["attempt"], 2)

    def test_an_unattempted_skip_preserves_reported_zero(self):
        ledger, _, _ = self.fold(None, [event(1, "step_done", step="optional", attempt=0, status="skipped")])
        self.assertEqual(ledger["projection"]["steps"]["optional"]["attempt"], 0)
        with self.assertRaises(RunEventInvalid):
            self.fold(None, [event(1, "step_started", step="optional", attempt=0)])

    def test_late_older_attempt_and_conflicting_final_do_not_rewrite_final_history(self):
        ledger, _, _ = self.fold(None, [event(4, "step_done", step="write", attempt=2, status="completed")])
        ledger, _, _ = self.fold(ledger, [event(1, "step_done", step="write", attempt=1, status="failed"),
                                         event(3, "step_done", step="write", attempt=2, status="failed")])
        self.assertEqual(ledger["projection"]["steps"]["write"]["status"], "completed")
        self.assertEqual(ledger["projection"]["steps"]["write"]["attempt"], 2)

    def test_terminal_run_does_not_reopen_in_the_same_ticket(self):
        ledger, _, _ = self.fold(None, [event(status="failed")])
        ledger, _, _ = self.fold(ledger, [event(2, status="running"),
            event(3, "step_started", step="write", attempt=2)])
        self.assertEqual(ledger["projection"]["status"], "failed")
        self.assertEqual(ledger["projection"]["steps"], {})

    def test_local_completed_cancelled_denied_status_always_wins(self):
        for status in ("completed", "cancelled", "denied"):
            with self.subTest(status=status):
                ledger, _, _ = self.fold(None, [event()], local_status=status, local_step="kept")
                self.assertEqual((ledger["projection"]["status"], ledger["projection"]["current_step"]), (status, "kept"))

    def test_new_ticket_reusing_attempt_one_retains_both_identities_and_old_facts_do_not_project(self):
        ledger, _, _ = self.fold(None, [event(1, "step_done", step="write", attempt=1, status="failed")])
        execution = {"execution_id": "ticket-2", "scope": {**FIXTURE["backend_run_execution_v1"]["scope"],
                                                          "expected_source_sha": "b" * 40}}
        ledger, _, _ = self.fold(ledger, [event(1, "step_started", step="write", attempt=1)], execution=execution, current="ticket-2")
        self.assertEqual(ledger["projection"]["steps"]["write"]["status"], "running")
        before = deepcopy(ledger["projection"])
        ledger, _, _ = self.fold(ledger, [event(2, status="completed")], current="ticket-2")
        self.assertEqual(ledger["projection"], before)
        self.assertEqual(len(ledger["events"]), 3)

    def test_a_malformed_saved_ledger_never_resets_or_drops_history(self):
        for saved in ({}, {"schema_version": 2}, {"schema_version": 1, "events": []}):
            with self.subTest(saved=saved), self.assertRaises(RunEventConflict):
                self.fold(saved, [event()])

    def test_a_retained_event_body_cannot_change_without_its_digest(self):
        ledger, _, _ = self.fold(None, [event()])
        next(iter(ledger["events"].values()))["event"]["status"] = "completed"
        with self.assertRaises(RunEventConflict):
            self.fold(ledger, [event(2)])
