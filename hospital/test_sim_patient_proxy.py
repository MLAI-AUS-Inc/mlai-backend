import json
from datetime import timedelta
from unittest.mock import Mock, patch
import uuid

import requests
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from .models import SimConversation, SimConversationTurn, SimParticipant
from .sim_security import InflightLease


URL = "/api/v1/hackathons/hospital/sim-patient/"
HEALTH_HACK_KEY = "health-hack-test-key"
ROO_URL = "http://10.126.0.5"
ROO_KEY = "roo-sim-patient-test-key"
PLAYER_A = "aaaaaaaa-1111-4111-8111-111111111111"
PLAYER_B = "bbbbbbbb-2222-4222-8222-222222222222"


def roo_reply(**overrides):
    payload = {
        "reply": "About two days now. I feel worse when I stand up.",
        "case_id": 1,
        "case_title": "Salt & Static",
        "patient_name": "Sasha 'Sash' Nguyen",
        "presenting_complaint": "Nausea, vomiting and abdominal cramps.",
        "is_guess": False,
        "correct": None,
        "diagnosis": None,
    }
    payload.update(overrides)
    return payload


def public_reply(**overrides):
    payload = {
        "reply": "About two days now. I feel worse when I stand up.",
        "case_id": 1,
        "case_title": "",
        "patient_name": "Sasha 'Sash' Nguyen",
        "presenting_complaint": "",
        "is_guess": False,
        "correct": None,
        "diagnosis": None,
        "suggested_action": None,
    }
    payload.update(overrides)
    return payload


def upstream_response(payload=None, *, status_code=200, raw=None):
    if raw is None:
        raw = json.dumps(payload if payload is not None else roo_reply()).encode("utf-8")
    response = Mock(
        ok=200 <= status_code < 300,
        status_code=status_code,
        headers={"content-length": str(len(raw))},
    )
    response.iter_content.return_value = [raw]
    response.close = Mock()
    return response


@override_settings(
    HEALTH_HACK_API_KEY=HEALTH_HACK_KEY,
    ROO_SERVICE_URL=ROO_URL,
    ROO_SIM_PATIENT_KEY=ROO_KEY,
    HEALTH_HACK_ACTIVE_CASE_ID=1,
    HEALTH_HACK_AI_RATE_LIMIT_MODE="observe",
    HEALTH_HACK_AI_BUDGET_MODE="observe",
    HEALTH_HACK_AI_KILL_SWITCH=False,
)
class SimPatientProxyTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def payload(self, **overrides):
        payload = {
            "question": "How long have you felt unwell?",
            "history": [],
            "player_id": PLAYER_A,
            "message_id": str(uuid.uuid4()),
            "role": "patient",
        }
        payload.update(overrides)
        return payload

    def post(self, data=None, *, key=HEALTH_HACK_KEY, source_ip=None, content_type=None):
        headers = {}
        if key is not None:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {key}"
        if source_ip:
            headers["HTTP_X_HEALTH_HACK_SOURCE_IP"] = source_ip
        if content_type:
            return self.client.post(
                URL,
                data=data,
                content_type=content_type,
                **headers,
            )
        return self.client.post(URL, data or self.payload(), format="json", **headers)

    def test_requires_dedicated_health_hack_key_before_parsing(self):
        oversized = "{" + ("x" * 20_000)
        response = self.post(
            oversized,
            key=None,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.post(self.payload(), key="wrong").status_code, 403)

    @patch("hospital.sim_patient_views.requests.post")
    def test_forwards_validated_turn_with_pinned_case_and_ignores_history(self, post):
        post.return_value = upstream_response()
        history = [
            {"role": "player" if i % 2 == 0 else "patient", "text": f"turn {i}"}
            for i in range(15)
        ]

        request_payload = self.payload(
            question="What makes the dizziness worse?",
            history=history,
            case_id=999,
        )
        response = self.post(request_payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, public_reply())
        post.assert_called_once_with(
            f"{ROO_URL}/api/sim-patient",
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {ROO_KEY}",
            },
            json={
                "question": "What makes the dizziness worse?",
                "history": [],
                "player_id": PLAYER_A,
                "message_id": request_payload["message_id"],
                "case_id": 1,
                "role": "patient",
            },
            timeout=(3, 24),
            stream=True,
        )
        turn = SimConversationTurn.objects.get()
        self.assertEqual(turn.player_text, "What makes the dizziness worse?")
        self.assertEqual(turn.public_response, public_reply())
        self.assertEqual(turn.response_status, 200)
        self.assertEqual(SimConversation.objects.get().case_id, 1)

    @patch("hospital.sim_patient_views.requests.post")
    def test_forwards_investigation_role_and_bounds_internal_metadata(self, post):
        post.return_value = upstream_response(roo_reply(
            patient_name="Nurse Priya",
            response_source="llm",
            model="gpt-5.6-terra",
            usage={"prompt_tokens": 100, "completion_tokens": 20},
            tool_calls=[{
                "name": "get_results",
                "arguments": {
                    "test_ids": ["bloods"],
                    "nested": {"not": "stored"},
                    "long": "x" * 201,
                },
            }],
        ))

        response = self.post(self.payload(
            question="Can I have the blood results?",
            role="nurse",
        ))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["patient_name"], "Dr Snow")
        turn = SimConversationTurn.objects.get()
        self.assertEqual(turn.model_name, "gpt-5.6-terra")
        self.assertEqual(turn.prompt_tokens, 100)
        self.assertEqual(turn.completion_tokens, 20)
        self.assertEqual(turn.tool_calls, [{
            "name": "get_results",
            "arguments": {"test_ids": ["bloods"]},
        }])
        self.assertNotIn("tool_calls", response.data)
        self.assertNotIn("usage", response.data)

    @patch("hospital.sim_patient_views.requests.post")
    def test_patient_tool_trace_is_never_stored(self, post):
        post.return_value = upstream_response(roo_reply(tool_calls=[{
            "name": "prepare_final_guess",
            "arguments": {"diagnosis": "hidden"},
        }]))
        self.assertEqual(self.post(self.payload()).status_code, 200)
        self.assertEqual(SimConversationTurn.objects.get().tool_calls, [])

    @patch("hospital.sim_patient_views.requests.post")
    def test_hardened_roo_may_blank_or_omit_internal_case_fields(self, post):
        omitted = roo_reply()
        omitted.pop("case_title")
        omitted.pop("presenting_complaint")
        post.side_effect = [
            upstream_response(roo_reply(case_title="", presenting_complaint="")),
            upstream_response(omitted),
        ]

        blank_response = self.post(self.payload(question="When did this start?"))
        omitted_response = self.post(self.payload(question="What does it feel like?"))

        self.assertEqual(blank_response.status_code, 200)
        self.assertEqual(omitted_response.status_code, 200)
        self.assertEqual(blank_response.data["case_title"], "")
        self.assertEqual(blank_response.data["presenting_complaint"], "")
        self.assertEqual(omitted_response.data["case_title"], "")
        self.assertEqual(omitted_response.data["presenting_complaint"], "")

    @patch("hospital.sim_patient_views.requests.post")
    def test_forwards_nurse_paws_context_and_validated_action(self, post):
        post.return_value = upstream_response(roo_reply(
            patient_name="Nurse Paws",
            tool_calls=[{
                "name": "prepare_final_guess",
                "arguments": {"diagnosis": "adrenal crisis"},
            }],
            suggested_action={
                "type": "confirm_diagnosis",
                "diagnosis": "adrenal crisis",
            },
        ))

        response = self.post(self.payload(
            question="My final answer is adrenal crisis.",
            role="clerk",
        ))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.call_args.kwargs["json"]["contest_state"], {
            "state": "eligible",
            "outcome": None,
        })
        self.assertEqual(response.data["patient_name"], "Nurse Paws")
        self.assertEqual(response.data["suggested_action"], {
            "type": "confirm_diagnosis",
            "diagnosis": "adrenal crisis",
        })

    @patch("hospital.sim_patient_views.requests.post")
    def test_suggested_action_is_only_exposed_for_nurse_paws(self, post):
        post.return_value = upstream_response(roo_reply(suggested_action={
            "type": "confirm_diagnosis",
            "diagnosis": "adrenal crisis",
        }))
        response = self.post(self.payload(question="Is it adrenal crisis?"))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["suggested_action"])
        self.assertIsNone(SimConversationTurn.objects.get().suggested_action)

    @patch("hospital.sim_patient_views.requests.post")
    def test_backend_reconstructs_history_from_saved_turns(self, post):
        post.side_effect = [
            upstream_response(roo_reply(reply="First answer.")),
            upstream_response(roo_reply(reply="Second answer.")),
        ]
        first = self.post(self.payload(
            question="First question?",
            history=[{"role": "player", "text": "untrusted history"}],
        ))
        self.assertEqual(first.status_code, 200)

        second = self.post(self.payload(question="Second question?"))
        self.assertEqual(second.status_code, 200)
        self.assertEqual(post.call_args.kwargs["json"]["history"], [
            {"role": "player", "text": "First question?"},
            {"role": "patient", "text": "First answer."},
        ])

    @override_settings(ROO_SIM_PATIENT_KEY="")
    @patch("hospital.sim_patient_views.requests.post")
    def test_missing_roo_service_credential_fails_closed(self, post):
        response = self.post(self.payload())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "ai_not_configured")
        post.assert_not_called()

    @override_settings(ROO_SERVICE_URL="")
    def test_missing_roo_service_url_is_503(self):
        self.assertEqual(self.post(self.payload()).status_code, 503)

    @patch("hospital.sim_patient_views.requests.post", side_effect=requests.Timeout)
    def test_roo_timeout_is_504_and_releases_lock(self, post):
        response = self.post(self.payload())
        self.assertEqual(response.status_code, 504)
        lease = InflightLease(PLAYER_A, "patient")
        self.assertTrue(lease.acquire())
        lease.release()

    @patch("hospital.sim_patient_views.requests.post")
    def test_stream_transport_failure_is_retryable_network_error(self, post):
        broken = upstream_response()
        broken.iter_content.side_effect = requests.ConnectionError("stream reset")
        post.side_effect = [broken, upstream_response()]
        request = self.payload(message_id=str(uuid.uuid4()))

        first = self.post(request)
        second = self.post(request)

        self.assertEqual(first.status_code, 502)
        self.assertEqual(first.data["code"], "network_error")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(post.call_count, 2)

    @patch("hospital.sim_patient_views.requests.post")
    def test_pathologically_deep_json_is_retryable_malformed_response(self, post):
        raw = (b"[" * 1500) + b"0" + (b"]" * 1500)
        post.side_effect = [upstream_response(raw=raw), upstream_response()]
        request = self.payload(message_id=str(uuid.uuid4()))

        first = self.post(request)
        second = self.post(request)

        self.assertEqual(first.status_code, 502)
        self.assertEqual(first.data["code"], "malformed_response")
        self.assertEqual(second.status_code, 200)

    @patch("hospital.sim_patient_views.requests.post")
    def test_transient_failure_can_retry_same_message_id_safely(self, post):
        post.side_effect = [requests.Timeout(), upstream_response()]
        request = self.payload(message_id=str(uuid.uuid4()))

        first = self.post(request)
        second = self.post(request)

        self.assertEqual(first.status_code, 504)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(SimConversationTurn.objects.count(), 1)
        turn = SimConversationTurn.objects.get()
        self.assertEqual(turn.response_status, 200)
        self.assertEqual(
            post.call_args.kwargs["json"]["message_id"],
            request["message_id"],
        )

    @override_settings(
        HEALTH_HACK_AI_RATE_LIMIT_MODE="enforce",
        HEALTH_HACK_AI_PARTICIPANT_BURST_LIMIT=1,
        HEALTH_HACK_AI_PARTICIPANT_10M_LIMIT=100,
        HEALTH_HACK_AI_PARTICIPANT_HOURLY_LIMIT=100,
    )
    @patch("hospital.sim_patient_views.requests.post", side_effect=requests.Timeout)
    def test_transient_retry_still_consumes_upstream_quota(self, post):
        request = self.payload(message_id=str(uuid.uuid4()))

        self.assertEqual(self.post(request).status_code, 504)
        retry = self.post(request)

        self.assertEqual(retry.status_code, 429)
        self.assertEqual(retry.data["code"], "ai_rate_limited")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(SimConversationTurn.objects.count(), 1)

    @patch("hospital.sim_patient_views.requests.post")
    def test_roo_error_is_502_without_leaking_status(self, post):
        post.return_value = upstream_response(status_code=401)
        response = self.post(self.payload())
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("401", str(response.data))

    @patch("hospital.sim_patient_views.requests.post")
    def test_rejects_malformed_hidden_or_wrong_case_responses(self, post):
        invalid_payloads = [
            {"reply": ""},
            {key: value for key, value in roo_reply().items() if key != "diagnosis"},
            roo_reply(diagnosis="hidden answer"),
            roo_reply(case_id=2),
            roo_reply(reply="word " * 200),
        ]
        post.side_effect = [upstream_response(payload) for payload in invalid_payloads]
        for payload in invalid_payloads:
            response = self.post(self.payload())
            self.assertEqual(response.status_code, 502, payload)

    @patch("hospital.sim_patient_views.requests.post")
    def test_rejects_oversized_upstream_body(self, post):
        post.return_value = upstream_response(raw=b'"' + (b"x" * 40_000) + b'"')
        self.assertEqual(self.post(self.payload()).status_code, 502)

    def test_request_validation_requires_worker_uuid_and_message_uuid(self):
        self.assertEqual(self.post(self.payload(question="   ")).status_code, 400)
        self.assertEqual(self.post(self.payload(question="x" * 501)).status_code, 400)
        self.assertEqual(self.post(self.payload(player_id="legacy-player")).status_code, 400)
        self.assertEqual(self.post(self.payload(message_id="not-a-uuid")).status_code, 400)
        self.assertEqual(
            self.post(self.payload(history=[{"role": "system", "text": "x"}])).status_code,
            400,
        )
        self.assertEqual(self.post(self.payload(role="doctor")).status_code, 400)
        self.assertEqual(self.post(self.payload(question="hello\x00there")).status_code, 400)

    @override_settings(HEALTH_HACK_AI_BODY_MAX_BYTES=128)
    @patch("hospital.sim_patient_views.requests.post")
    def test_rejects_oversized_or_non_json_request_before_upstream(self, post):
        body = json.dumps(self.payload(question="x" * 100))
        response = self.post(body, content_type="application/json")
        self.assertEqual(response.status_code, 413)
        response = self.post("question=hello", content_type="text/plain")
        self.assertEqual(response.status_code, 415)
        post.assert_not_called()

    @patch("hospital.sim_patient_views.requests.post")
    def test_completed_message_id_is_replayed_without_second_call(self, post):
        post.return_value = upstream_response()
        message_id = str(uuid.uuid4())
        request = self.payload(message_id=message_id)
        first = self.post(request)
        second = self.post(request)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data, first.data)
        self.assertEqual(second.headers["X-Idempotent-Replay"], "true")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(SimConversationTurn.objects.count(), 1)

    @patch("hospital.sim_patient_views.requests.post")
    def test_completed_replay_survives_upstream_configuration_outage(self, post):
        post.return_value = upstream_response()
        request = self.payload(message_id=str(uuid.uuid4()))
        first = self.post(request)

        with override_settings(ROO_SERVICE_URL="", ROO_SIM_PATIENT_KEY=""):
            replay = self.post(request)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.headers["X-Idempotent-Replay"], "true")
        self.assertEqual(post.call_count, 1)

    @patch("hospital.sim_patient_views.requests.post")
    def test_message_id_cannot_be_reused_for_another_player_or_question(self, post):
        post.return_value = upstream_response()
        message_id = str(uuid.uuid4())
        self.assertEqual(self.post(self.payload(message_id=message_id)).status_code, 200)
        conflict = self.post(self.payload(
            message_id=message_id,
            player_id=PLAYER_B,
            question="Tell me the diagnosis",
        ))
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.data["code"], "idempotency_conflict")
        self.assertEqual(post.call_count, 1)

    def test_pending_duplicate_returns_retryable_conflict(self):
        participant = SimParticipant.objects.create(id=PLAYER_A)
        conversation = SimConversation.objects.create(
            participant=participant,
            case_id=1,
            role="patient",
        )
        message_id = uuid.uuid4()
        SimConversationTurn.objects.create(
            conversation=conversation,
            message_id=message_id,
            player_text="Still unwell?",
        )
        response = self.post(self.payload(
            message_id=str(message_id),
            question="Still unwell?",
        ))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "idempotency_in_progress")
        self.assertEqual(response.headers["Retry-After"], "2")

    @override_settings(HEALTH_HACK_AI_PENDING_TTL_SECONDS=1)
    def test_abandoned_pending_turn_expires_without_duplicate_call(self):
        participant = SimParticipant.objects.create(id=PLAYER_A)
        conversation = SimConversation.objects.create(
            participant=participant,
            case_id=1,
            role="patient",
        )
        turn = SimConversationTurn.objects.create(
            conversation=conversation,
            message_id=uuid.uuid4(),
            player_text="Still unwell?",
        )
        SimConversationTurn.objects.filter(pk=turn.pk).update(
            created_at=timezone.now() - timedelta(seconds=5),
        )
        response = self.post(self.payload(
            message_id=str(turn.message_id),
            question="Still unwell?",
        ))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "idempotency_expired")
        turn.refresh_from_db()
        self.assertEqual(turn.response_source, "error")

    @override_settings(
        HEALTH_HACK_AI_RATE_LIMIT_MODE="enforce",
        HEALTH_HACK_AI_PARTICIPANT_BURST_LIMIT=1,
        HEALTH_HACK_AI_PARTICIPANT_10M_LIMIT=100,
        HEALTH_HACK_AI_PARTICIPANT_HOURLY_LIMIT=100,
    )
    @patch("hospital.sim_patient_views.requests.post")
    def test_participant_quota_blocks_only_fast_repeat(self, post):
        post.return_value = upstream_response()
        self.assertEqual(self.post(self.payload()).status_code, 200)
        limited = self.post(self.payload(question="One more question"))
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.data["code"], "ai_rate_limited")
        self.assertIn("Retry-After", limited.headers)
        self.assertEqual(post.call_count, 1)

    @override_settings(
        HEALTH_HACK_AI_RATE_LIMIT_MODE="observe",
        HEALTH_HACK_AI_PARTICIPANT_BURST_LIMIT=1,
        HEALTH_HACK_AI_PARTICIPANT_10M_LIMIT=100,
        HEALTH_HACK_AI_PARTICIPANT_HOURLY_LIMIT=100,
    )
    @patch("hospital.sim_patient_views.requests.post")
    def test_observation_mode_never_interrupts_gameplay(self, post):
        post.side_effect = [upstream_response(), upstream_response()]
        self.assertEqual(self.post(self.payload()).status_code, 200)
        self.assertEqual(self.post(self.payload(question="One more question")).status_code, 200)
        self.assertEqual(post.call_count, 2)

    @override_settings(
        HEALTH_HACK_AI_RATE_LIMIT_MODE="enforce",
        HEALTH_HACK_AI_PARTICIPANT_BURST_LIMIT=100,
        HEALTH_HACK_AI_NETWORK_BURST_LIMIT=1,
        HEALTH_HACK_AI_NETWORK_10M_LIMIT=100,
        HEALTH_HACK_AI_NETWORK_HOURLY_LIMIT=100,
    )
    @patch("hospital.sim_patient_views.requests.post")
    def test_source_network_quota_survives_cookie_rotation(self, post):
        post.return_value = upstream_response()
        self.assertEqual(
            self.post(self.payload(), source_ip="203.0.113.10").status_code,
            200,
        )
        response = self.post(
            self.payload(player_id=PLAYER_B),
            source_ip="203.0.113.200",
        )
        self.assertEqual(response.status_code, 429)  # same /24
        self.assertEqual(post.call_count, 1)
        self.assertFalse(SimParticipant.objects.filter(id=PLAYER_B).exists())
        self.assertEqual(SimConversation.objects.count(), 1)

    @patch("hospital.sim_patient_views.requests.post")
    def test_one_inflight_request_per_participant_across_npc_roles(self, post):
        lease = InflightLease(PLAYER_A, "patient")
        self.assertTrue(lease.acquire())
        try:
            response = self.post(self.payload(role="nurse"))
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.data["code"], "ai_request_in_flight")
            post.assert_not_called()
        finally:
            lease.release()

    @override_settings(
        HEALTH_HACK_AI_BUDGET_MODE="enforce",
        HEALTH_HACK_AI_DAILY_CALL_LIMIT=1,
        HEALTH_HACK_AI_DAILY_TOKEN_LIMIT=1000,
        HEALTH_HACK_AI_MAX_PROMPT_TOKENS=100,
        HEALTH_HACK_AI_MAX_COMPLETION_TOKENS=100,
    )
    @patch("hospital.sim_patient_views.requests.post")
    def test_daily_call_budget_circuit_breaker(self, post):
        post.return_value = upstream_response()
        self.assertEqual(self.post(self.payload()).status_code, 200)
        blocked = self.post(self.payload(
            player_id=PLAYER_B,
            question="Can I ask again?",
        ))
        self.assertEqual(blocked.status_code, 503)
        self.assertEqual(blocked.data["code"], "ai_budget_exhausted")
        self.assertEqual(post.call_count, 1)
        self.assertFalse(SimParticipant.objects.filter(id=PLAYER_B).exists())
        self.assertEqual(SimConversation.objects.count(), 1)

    @override_settings(
        HEALTH_HACK_AI_BUDGET_MODE="enforce",
        HEALTH_HACK_AI_DAILY_CALL_LIMIT=100,
        HEALTH_HACK_AI_DAILY_TOKEN_LIMIT=12,
        HEALTH_HACK_AI_MAX_PROMPT_TOKENS=8,
        HEALTH_HACK_AI_MAX_COMPLETION_TOKENS=4,
    )
    @patch("hospital.sim_patient_views.requests.post")
    def test_reported_tokens_trip_future_call_budget(self, post):
        post.return_value = upstream_response(roo_reply(
            usage={"prompt_tokens": 8, "completion_tokens": 3},
        ))
        self.assertEqual(self.post(self.payload()).status_code, 200)
        blocked = self.post(self.payload(question="Can I ask again?"))
        self.assertEqual(blocked.status_code, 503)
        self.assertEqual(post.call_count, 1)

    @override_settings(
        HEALTH_HACK_AI_BUDGET_MODE="enforce",
        HEALTH_HACK_AI_DAILY_CALL_LIMIT=100,
        HEALTH_HACK_AI_DAILY_TOKEN_LIMIT=10,
        HEALTH_HACK_AI_MAX_PROMPT_TOKENS=6,
        HEALTH_HACK_AI_MAX_COMPLETION_TOKENS=4,
    )
    @patch("hospital.sim_patient_views.requests.post")
    def test_missing_usage_keeps_worst_case_token_reservation(self, post):
        post.return_value = upstream_response(roo_reply())

        self.assertEqual(self.post(self.payload()).status_code, 200)
        blocked = self.post(self.payload(question="Can I ask again?"))

        self.assertEqual(blocked.status_code, 503)
        self.assertEqual(blocked.data["code"], "ai_budget_exhausted")
        self.assertEqual(post.call_count, 1)

    @override_settings(HEALTH_HACK_AI_KILL_SWITCH=True)
    @patch("hospital.sim_patient_views.requests.post")
    def test_kill_switch_prevents_upstream_call(self, post):
        response = self.post(self.payload())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "ai_temporarily_disabled")
        post.assert_not_called()
        self.assertEqual(SimConversationTurn.objects.count(), 0)
        self.assertEqual(SimConversation.objects.count(), 0)
        self.assertEqual(SimParticipant.objects.count(), 0)

    def test_lock_release_never_deletes_a_new_owner(self):
        lease = InflightLease(PLAYER_A, "patient")
        self.assertTrue(lease.acquire())
        cache.set(lease.key, "replacement-owner", timeout=30)
        lease.release()
        self.assertEqual(cache.get(lease.key), "replacement-owner")
