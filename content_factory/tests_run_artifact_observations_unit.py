"""Actual CF-produced typed candidates never confer delivery by hash alone."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from .run_artifact_observations import ArtifactObservationInvalid, _digest, validate_evidence
from .run_ownership import OWNER_EXECUTION_KEY, OWNER_RESULT_KEY, WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY


def fixture():
    return json.loads((Path(__file__).parent / "fixtures/r3_typed_delivery_evidence.json").read_text())


def owner(value):
    wire = value[WIRE_REQUEST_KEY]
    current = value[WIRE_EXECUTION_KEY]
    tickets = {record[WIRE_EXECUTION_KEY]["execution_id"]: deepcopy(record[WIRE_EXECUTION_KEY]) for record in value["evidence"]}
    tickets[current["execution_id"]] = deepcopy(current)
    return SimpleNamespace(run_id=value["run_id"], organization_id=int(wire["organization_id"]),
        workflow=wire["workflow"], domain=wire["domain"], github_repo=wire["github_repo"], run_request={},
        result={OWNER_RESULT_KEY: {"schema_version": 1,
            "identity": {key: item for key, item in wire.items() if key != "event_protocol"},
            "authority": {"backend_facts": True, "event_protocol": 1}},
            OWNER_EXECUTION_KEY: {"current_execution_id": current["execution_id"], "tickets": tickets}})


def rehash(value):
    for record in value["evidence"]:
        record["payload_sha256"] = _digest(record["payload"])
        record["evidence_sha256"] = _digest({key: item for key, item in record.items() if key != "evidence_sha256"})
    return value


class TypedArtifactCandidateTests(SimpleTestCase):
    def test_trickling_evidence_and_canonical_reads_stop_at_total_deadline_and_close(self):
        from .run_artifact_observations import fetch_evidence
        from .run_verified_delivery import _fetch_review
        run = owner(fixture())
        for fetch, module in ((fetch_evidence, "run_artifact_observations"), (_fetch_review, "run_verified_delivery")):
            response = Mock(status_code=200)
            response.iter_content.side_effect = lambda **kwargs: iter([b"{", b'"'])
            with self.subTest(fetch=module), \
                    patch("content_factory.vibe_marketing_views._content_factory_remote_config", return_value={"enabled": True, "base_url": "https://cf.fixture.invalid"}), \
                    patch("requests.get", return_value=response), \
                    patch("content_factory." + module + ".monotonic", side_effect=[0, 1, 16]), \
                    self.assertRaises(ArtifactObservationInvalid):
                fetch(run)
            response.close.assert_called_once()
            self.assertEqual(response.iter_content.call_args.kwargs["chunk_size"], 1)

    def test_actual_cf_wrapper_only_reaches_typed_bridge_and_never_the_v1_projector(self):
        from .run_owned_observations import maybe_receive_owned_observation
        value = json.loads((Path(__file__).parent / "fixtures/r3_owned_delivery_wrapper.json").read_text())["body"]
        run = owner({**value, "run_id": value["delivery_v1"]["run_id"], "evidence": []})
        before = deepcopy(run.__dict__)
        with patch("content_factory.run_owned_observations.admitted_run_for_key", return_value=run), \
                patch("content_factory.run_verified_delivery.project_verified_delivery", return_value={"status": "observation_only"}) as bridge, \
                patch("content_factory.setup_delivery_receiver._project_state") as legacy:
            response = maybe_receive_owned_observation(value, expected_kind="run_snapshot", run_id=run.run_id)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["disposition"], "observation_only")
        bridge.assert_called_once_with(run.run_id)
        legacy.assert_not_called()
        self.assertEqual(run.__dict__, before)

    def test_cf_wrapper_creation_ticket_cannot_be_replaced_or_unknown_fields_accepted(self):
        from .run_owned_observations import maybe_receive_owned_observation
        original = json.loads((Path(__file__).parent / "fixtures/r3_owned_delivery_wrapper.json").read_text())["body"]
        run = owner({**original, "run_id": original["delivery_v1"]["run_id"], "evidence": []})
        for mode in ("ticket", "extra", "scope"):
            value = deepcopy(original)
            if mode == "ticket":
                value[WIRE_EXECUTION_KEY]["execution_id"] = "invented-ticket"
            elif mode == "extra":
                value["status"] = "completed"
            else:
                value[WIRE_EXECUTION_KEY]["scope"]["operation_attempt"] += 1
            with patch("content_factory.run_owned_observations.admitted_run_for_key", return_value=run), \
                    patch("content_factory.run_verified_delivery.project_verified_delivery") as bridge:
                self.assertEqual(maybe_receive_owned_observation(value).status_code, 409)
                bridge.assert_not_called()

    def test_actual_producer_fixture_is_compatible_without_any_run_or_approval_effect(self):
        value = fixture()
        run = owner(value)
        before = deepcopy(run.__dict__)
        accepted = validate_evidence(run, value)
        self.assertEqual(len(accepted.records), 3)
        self.assertEqual(run.__dict__, before)
        self.assertEqual({row["kind"] for row in accepted.records}, {"article_body_v1", "draft_ready_v1", "publish_pr_v1"})

    def test_changed_body_bytes_refuse_even_if_transport_hashes_are_recomputed(self):
        value = fixture()
        run = owner(value)
        value["evidence"][0]["payload"]["body"] += " changed"
        with self.assertRaises(ArtifactObservationInvalid):
            validate_evidence(run, rehash(value))

    def test_new_valid_content_hash_is_still_only_an_unverified_candidate(self):
        import hashlib
        value = fixture()
        run = owner(value)
        record = value["evidence"][0]
        record["payload"]["body"] = "Another syntactically valid body"
        record["payload"]["article_sha256"] = hashlib.sha256(record["payload"]["body"].encode()).hexdigest()
        before = deepcopy(run.__dict__)
        self.assertEqual(validate_evidence(run, rehash(value)).records[0]["payload"]["body"], "Another syntactically valid body")
        self.assertEqual(run.__dict__, before)

    def test_exact_retained_creation_ticket_may_be_read_after_current_scope_changes(self):
        value = fixture()
        run = owner(value)
        new = {**deepcopy(value[WIRE_EXECUTION_KEY]), "execution_id": "control:new-current", "generation": 1,
               "request_digest": "d" * 64}
        run.result[OWNER_EXECUTION_KEY]["tickets"][new["execution_id"]] = new
        run.result[OWNER_EXECUTION_KEY]["current_execution_id"] = new["execution_id"]
        value[WIRE_EXECUTION_KEY] = deepcopy(new)
        self.assertEqual(len(validate_evidence(run, value).records), 3)
        value["evidence"][0][WIRE_EXECUTION_KEY]["execution_id"] = "invented-creation"
        with self.assertRaises(ArtifactObservationInvalid):
            validate_evidence(run, rehash(value))

    def test_forged_current_tenant_ticket_or_source_has_no_candidate_admission(self):
        for field, changed in (("organization_id", "999"), ("workflow", "foreign"), ("dispatch_key", "foreign")):
            value = fixture()
            run = owner(value)
            value[WIRE_REQUEST_KEY][field] = changed
            with self.subTest(field=field), self.assertRaises(ArtifactObservationInvalid):
                validate_evidence(run, value)
        value = fixture()
        run = owner(value)
        value[WIRE_EXECUTION_KEY]["scope"]["expected_source_sha"] = "e" * 40
        with self.assertRaises(ArtifactObservationInvalid):
            validate_evidence(run, value)

    def test_duplicates_unknown_fields_boolean_ids_and_raw_worker_text_refuse(self):
        for mode in ("duplicate", "text", "bool", "extra", "hash"):
            value = fixture()
            run = owner(value)
            if mode == "duplicate":
                value["evidence"][1] = deepcopy(value["evidence"][0])
            elif mode == "text":
                value["evidence"][2]["payload"]["message"] = "untrusted worker text"
            elif mode == "bool":
                value["evidence"][2]["payload"]["number"] = True
            elif mode == "extra":
                value["status"] = "completed"
            else:
                value["evidence"][0]["source_sha256"] = "not-a-hash"
            with self.subTest(mode=mode), self.assertRaises(ArtifactObservationInvalid):
                validate_evidence(run, rehash(value))
