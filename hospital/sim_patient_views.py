"""Authenticated Health Hack gateway to Roo's ward NPC agents."""

from __future__ import annotations

from datetime import timedelta
import json
import logging
import math
import re
import time
import unicodedata
import uuid

import requests
from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasHealthHackApiKey
from .models import (
    SimConversation,
    SimConversationTurn,
    SimDiagnosisGuess,
    SimParticipant,
)
from .sim_security import (
    LimitedJSONParser,
    acquire_inflight,
    consume_rate_limits,
    read_limited_json,
    reserve_global_call,
    source_network_key,
)


logger = logging.getLogger(__name__)

ROO_PATH = "/api/sim-patient"
CONNECT_TIMEOUT_SECONDS = 3
# Gunicorn's production worker timeout is 30 seconds. Leave enough time for
# Django to turn a slow Roo call into a controlled 504 instead of losing the
# worker mid-request.
READ_TIMEOUT_SECONDS = 24
MAX_HISTORY_TURNS = 12
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ARGUMENT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
RETRYABLE_TURN_ERRORS = {
    "timeout",
    "network_error",
    "malformed_response",
    "abandoned_pending",
}


class PatientTurnSerializer(serializers.Serializer):
    """Rolling-deploy compatibility only; browser history is never trusted."""

    role = serializers.ChoiceField(choices=("player", "patient"))
    text = serializers.CharField(max_length=1000, trim_whitespace=True)


class SimPatientRequestSerializer(serializers.Serializer):
    question = serializers.CharField(max_length=500, trim_whitespace=True)
    history = PatientTurnSerializer(many=True, required=False, default=list)
    # Accepted during a rolling Worker deployment but deliberately ignored.
    case_id = serializers.IntegerField(min_value=1, required=False)
    player_id = serializers.UUIDField(required=True)
    role = serializers.ChoiceField(
        choices=("patient", "nurse", "clerk"),
        required=False,
        default="patient",
    )
    message_id = serializers.UUIDField(required=True)

    def validate_question(self, value):
        for character in value:
            if unicodedata.category(character) == "Cc" and character not in "\n\r\t":
                raise serializers.ValidationError("question contains invalid control characters")
        return value

    def validate_history(self, value):
        # Bound legacy input before discarding it. The canonical history always
        # comes from SimConversationTurn records below.
        return value[-MAX_HISTORY_TURNS:]


def _error_payload(code: str, message: str, retry_after_seconds: int | None = None) -> dict:
    payload = {"code": code, "message": message, "detail": message}
    if retry_after_seconds:
        payload["retry_after_seconds"] = int(retry_after_seconds)
    return payload


def _error_response(
    code: str,
    message: str,
    http_status: int,
    *,
    retry_after_seconds: int | None = None,
) -> Response:
    headers = None
    if retry_after_seconds:
        headers = {"Retry-After": str(max(1, int(retry_after_seconds)))}
    return Response(
        _error_payload(code, message, retry_after_seconds),
        status=http_status,
        headers=headers,
    )


def _history_from_conversation(conversation):
    """Return the most recent six completed exchanges as 12 wire turns."""

    exchanges = list(
        conversation.turns
        .exclude(response_source=SimConversationTurn.SOURCE_PENDING)
        .exclude(npc_text="")
        .order_by("-created_at")[:6]
    )
    history = []
    for turn in reversed(exchanges):
        history.extend([
            {"role": "player", "text": turn.player_text},
            {"role": "patient", "text": turn.npc_text},
        ])
    return history


def _contest_state(participant, case_id):
    """Return only the read-only contest context Nurse Paws needs."""

    guess = SimDiagnosisGuess.objects.filter(
        participant=participant,
        case_id=case_id,
    ).first()
    if guess is None:
        return {"state": "eligible", "outcome": None}
    if not guess.is_correct:
        state = "locked"
    elif guess.outcome == SimDiagnosisGuess.OUTCOME_PENDING_CLAIM:
        state = "awaiting_claim"
    else:
        state = "completed"
    return {"state": state, "outcome": guess.outcome}


def _clean_text(value, *, max_length: int, multiline: bool = False):
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFC", value).strip()
    if not value or len(value) > max_length:
        return None
    allowed_controls = "\n\r\t" if multiline else ""
    if any(
        unicodedata.category(character) in {"Cc", "Cf"}
        and character not in allowed_controls
        for character in value
    ):
        return None
    return value


def _sanitize_argument_value(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value if -1_000_000 <= value <= 1_000_000 else None
    if isinstance(value, float):
        return value if math.isfinite(value) and abs(value) <= 1_000_000 else None
    if isinstance(value, str):
        return _clean_text(value, max_length=200, multiline=False)
    if isinstance(value, list) and len(value) <= 16:
        sanitized = []
        for item in value:
            projected = _sanitize_argument_value(item)
            if projected is None:
                return None
            sanitized.append(projected)
        return sanitized
    return None


def _sanitized_tool_calls(payload, role):
    # Sash never has tools. Even if a compromised upstream supplies a trace,
    # the gateway neither stores nor projects it.
    if role == SimConversation.ROLE_PATIENT:
        return []
    calls = payload.get("tool_calls")
    if calls is None:
        return []
    if not isinstance(calls, list):
        return []
    sanitized = []
    for call in calls[:8]:
        if not isinstance(call, dict):
            continue
        name = call.get("name")
        arguments = call.get("arguments")
        if not isinstance(name, str) or not TOOL_NAME_RE.fullmatch(name):
            continue
        if not isinstance(arguments, dict) or len(arguments) > 8:
            arguments = {}
        projected_arguments = {}
        for key, value in arguments.items():
            if not isinstance(key, str) or not ARGUMENT_NAME_RE.fullmatch(key):
                continue
            projected = _sanitize_argument_value(value)
            if projected is not None:
                projected_arguments[key] = projected
        sanitized.append({"name": name, "arguments": projected_arguments})
    if len(json.dumps(sanitized, ensure_ascii=False)) > 4096:
        return []
    return sanitized


def _sanitized_action(payload, role):
    action = payload.get("suggested_action")
    if role != SimConversation.ROLE_CLERK or not isinstance(action, dict):
        return None
    if action.get("type") != "confirm_diagnosis":
        return None
    diagnosis = _clean_text(action.get("diagnosis"), max_length=200, multiline=False)
    if diagnosis is None:
        return None
    return {"type": "confirm_diagnosis", "diagnosis": diagnosis}


def _sanitized_usage(payload):
    usage = payload.get("usage")
    if usage is None:
        return None, None
    if not isinstance(usage, dict):
        return None, None

    def token_value(name, maximum):
        value = usage.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value if 0 <= value <= maximum else None

    max_prompt = int(getattr(settings, "HEALTH_HACK_AI_MAX_PROMPT_TOKENS", 100_000))
    max_completion = int(getattr(settings, "HEALTH_HACK_AI_MAX_COMPLETION_TOKENS", 8_192))
    prompt = token_value("prompt_tokens", max_prompt)
    completion = token_value("completion_tokens", max_completion)
    return prompt, completion


def _project_roo_reply(payload, role, active_case_id):
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("case_id"), bool) or payload.get("case_id") != active_case_id:
        return None
    # Roo's public contract must explicitly carry the non-adjudication fields.
    # Treating an absent key like a present JSON null would let accidental
    # schema drift pass through this trust boundary unnoticed.
    if "correct" not in payload or "diagnosis" not in payload:
        return None
    if payload.get("correct") is not None or payload.get("diagnosis") is not None:
        return None
    if not isinstance(payload.get("is_guess"), bool):
        return None

    reply = _clean_text(
        payload.get("reply"),
        max_length=int(getattr(settings, "HEALTH_HACK_AI_REPLY_MAX_CHARS", 1500)),
        multiline=True,
    )
    if reply is None:
        return None
    max_words = int(getattr(settings, "HEALTH_HACK_AI_REPLY_MAX_WORDS", 160))
    if max_words > 0 and len(reply.split()) > max_words:
        return None

    # Legacy Roo versions supplied case_title and presenting_complaint here.
    # Hardened Roo deliberately omits or blanks both internal clinical fields.
    # Neither field is needed to validate a spoken reply, and neither is ever
    # projected to the browser, so tolerate both wire shapes during rollout.
    upstream_name = _clean_text(payload.get("patient_name"), max_length=100)
    if upstream_name is None:
        return None

    if role == SimConversation.ROLE_NURSE:
        public_name = "Dr Snow"
    elif role == SimConversation.ROLE_CLERK:
        public_name = "Nurse Paws"
    else:
        public_name = upstream_name

    suggested_action = _sanitized_action(payload, role)
    public_response = {
        "reply": reply,
        "case_id": active_case_id,
        "case_title": "",
        "patient_name": public_name,
        "presenting_complaint": "",
        "is_guess": payload["is_guess"],
        "correct": None,
        "diagnosis": None,
        "suggested_action": suggested_action,
    }
    prompt_tokens, completion_tokens = _sanitized_usage(payload)
    metadata = {
        "response_source": (
            payload.get("response_source")
            if payload.get("response_source") in {
                SimConversationTurn.SOURCE_LLM,
                SimConversationTurn.SOURCE_DETERMINISTIC,
            }
            else SimConversationTurn.SOURCE_LLM
        ),
        "model_name": _clean_text(payload.get("model"), max_length=100) or "",
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "tool_calls": _sanitized_tool_calls(payload, role),
        "suggested_action": suggested_action,
    }
    return public_response, metadata


def _read_limited_json(upstream):
    max_bytes = int(getattr(settings, "HEALTH_HACK_AI_UPSTREAM_MAX_BYTES", 32 * 1024))
    return read_limited_json(upstream, max_bytes=max_bytes)


def _existing_turn_disposition(turn, *, participant_id, case_id, role, question):
    """Return (response, retryable_turn) after binding an id to its owner/text."""

    conversation = turn.conversation
    if (
        conversation.participant_id != participant_id
        or conversation.case_id != case_id
        or conversation.role != role
        or turn.player_text != question
    ):
        return _error_response(
            "idempotency_conflict",
            "message identifier was already used",
            status.HTTP_409_CONFLICT,
        ), None

    if (
        turn.response_status == status.HTTP_200_OK
        and isinstance(turn.public_response, dict)
    ):
        return Response(
            turn.public_response,
            status=turn.response_status,
            headers={"X-Idempotent-Replay": "true"},
        ), None

    if turn.response_source == SimConversationTurn.SOURCE_PENDING:
        stale_after = int(
            getattr(settings, "HEALTH_HACK_AI_PENDING_TTL_SECONDS", 35)
        )
        if timezone.now() - turn.created_at > timedelta(seconds=max(1, stale_after)):
            payload = _error_payload(
                "idempotency_expired",
                "the previous request could not be confirmed; please send it again",
                1,
            )
            now = timezone.now()
            SimConversationTurn.objects.filter(
                pk=turn.pk,
                response_source=SimConversationTurn.SOURCE_PENDING,
            ).update(
                response_source=SimConversationTurn.SOURCE_ERROR,
                error_code="abandoned_pending",
                completed_at=now,
                public_response=payload,
                response_status=status.HTTP_409_CONFLICT,
            )
            return Response(
                payload,
                status=status.HTTP_409_CONFLICT,
                headers={"Retry-After": "1"},
            ), None
        return _error_response(
            "idempotency_in_progress",
            "this message is still being answered",
            status.HTTP_409_CONFLICT,
            retry_after_seconds=2,
        ), None

    if (
        turn.response_source == SimConversationTurn.SOURCE_ERROR
        and (
            turn.error_code in RETRYABLE_TURN_ERRORS
            or turn.error_code.startswith("upstream_")
        )
    ):
        # The caller reuses the same message_id. The per-participant/role lease
        # and the atomic ERROR -> PENDING claim below ensure only one retry can
        # leave Django. message_id is also forwarded to Roo so its boundary can
        # deduplicate an ambiguous timeout.
        return None, turn

    if isinstance(turn.public_response, dict) and turn.response_status:
        return Response(
            turn.public_response,
            status=turn.response_status,
            headers={"X-Idempotent-Replay": "true"},
        ), None

    # Legacy completed rows predate stored public response envelopes. Never
    # make a second paid call for them.
    return _error_response(
        "idempotency_unavailable",
        "this message was already processed",
        status.HTTP_409_CONFLICT,
    ), None


def _claim_retryable_turn(turn):
    updated = SimConversationTurn.objects.filter(
        pk=turn.pk,
        response_source=SimConversationTurn.SOURCE_ERROR,
    ).filter(
        # Q would be clearer for a large policy, but this small union remains
        # explicit and keeps arbitrary terminal errors non-retryable.
        error_code__in=(
            *RETRYABLE_TURN_ERRORS,
            turn.error_code if turn.error_code.startswith("upstream_") else "",
        ),
    ).update(
        response_source=SimConversationTurn.SOURCE_PENDING,
        npc_text="",
        model_name="",
        prompt_tokens=None,
        completion_tokens=None,
        tool_calls=[],
        suggested_action=None,
        latency_ms=None,
        error_code="",
        completed_at=None,
        public_response=None,
        response_status=None,
        created_at=timezone.now(),
    )
    if not updated:
        return None
    turn.refresh_from_db()
    return turn


def _fail_turn(
    turn,
    conversation,
    *,
    started,
    code,
    message,
    http_status,
    public_code=None,
):
    completed_at = timezone.now()
    payload = _error_payload(public_code or code, message)
    turn.response_source = SimConversationTurn.SOURCE_ERROR
    turn.error_code = code[:64]
    turn.latency_ms = max(0, round((time.monotonic() - started) * 1000))
    turn.completed_at = completed_at
    turn.public_response = payload
    turn.response_status = http_status
    turn.save(update_fields=[
        "response_source",
        "error_code",
        "latency_ms",
        "completed_at",
        "public_response",
        "response_status",
    ])
    SimConversation.objects.filter(pk=conversation.pk).update(last_turn_at=completed_at)
    return Response(payload, status=http_status)


class SimPatientProxyView(APIView):
    """POST sim-patient/ — securely proxy one ward-NPC turn to Roo over the VPC."""

    authentication_classes = []
    permission_classes = [HasHealthHackApiKey]
    parser_classes = [LimitedJSONParser]

    def post(self, request):
        serializer = SimPatientRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        participant_id = data["player_id"]
        # The Worker/Roo may still send a case_id during a rolling deployment,
        # but backend configuration is the only authority.
        case_id = int(getattr(settings, "HEALTH_HACK_ACTIVE_CASE_ID", 1))

        # A completed idempotent replay is a read-only fast path. It remains
        # available when Roo is down and consumes neither quota nor DB writes.
        existing = (
            SimConversationTurn.objects.select_related("conversation")
            .filter(message_id=data["message_id"])
            .first()
        )
        if existing is not None:
            existing_response, retryable_turn = _existing_turn_disposition(
                existing,
                participant_id=participant_id,
                case_id=case_id,
                role=data["role"],
                question=data["question"],
            )
            if existing_response is not None:
                return existing_response
        else:
            retryable_turn = None

        base_url = str(getattr(settings, "ROO_SERVICE_URL", "") or "").rstrip("/")
        roo_key = str(getattr(settings, "ROO_SIM_PATIENT_KEY", "") or "")
        if not base_url or not roo_key:
            return _error_response(
                "ai_not_configured",
                "simulated patient service is not configured",
                status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # The underlying lease is participant-wide (the role is diagnostic
        # context only), preventing overlapping calls across NPCs and tabs.
        lease, lease_decision = acquire_inflight(str(participant_id), data["role"])
        if not lease_decision.allowed:
            return _error_response(
                lease_decision.code,
                "another message is already being answered",
                status.HTTP_409_CONFLICT,
                retry_after_seconds=lease_decision.retry_after_seconds,
            )

        budget_reservation = None
        upstream_attempted = False
        try:
            # Close the race between the first lookup and acquiring the lease.
            existing = (
                SimConversationTurn.objects.select_related("conversation")
                .filter(message_id=data["message_id"])
                .first()
            )
            if existing is not None:
                existing_response, retryable_turn = _existing_turn_disposition(
                    existing,
                    participant_id=participant_id,
                    case_id=case_id,
                    role=data["role"],
                    question=data["question"],
                )
                if existing_response is not None:
                    return existing_response

            # Every attempt that can leave Django consumes quota, including a
            # same-message retry after a timeout. Completed/in-flight replays
            # returned above consume nothing, while a script cannot recycle one
            # failed message_id to bypass the participant ceiling.
            rate_decision = consume_rate_limits(
                str(participant_id),
                source_network_key(request),
            )
            if not rate_decision.allowed:
                return _error_response(
                    rate_decision.code,
                    "too many messages; please wait a moment",
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    retry_after_seconds=rate_decision.retry_after_seconds,
                )

            budget_reservation, budget_decision = reserve_global_call()
            if not budget_decision.allowed:
                return _error_response(
                    budget_decision.code,
                    "the AI service is taking a short break",
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    retry_after_seconds=budget_decision.retry_after_seconds,
                )

            if retryable_turn is not None:
                conversation = retryable_turn.conversation
                participant = conversation.participant
                SimParticipant.objects.filter(pk=participant.pk).update(
                    last_seen_at=timezone.now(),
                )
                turn = _claim_retryable_turn(retryable_turn)
                if turn is None:
                    return _error_response(
                        "idempotency_in_progress",
                        "this message is still being answered",
                        status.HTTP_409_CONFLICT,
                        retry_after_seconds=2,
                    )
            else:
                # Anonymous participant/transcript rows are created only after
                # every cache-backed admission guard has passed.
                participant, _ = SimParticipant.objects.get_or_create(id=participant_id)
                SimParticipant.objects.filter(pk=participant.pk).update(
                    last_seen_at=timezone.now(),
                )
                conversation, _ = SimConversation.objects.get_or_create(
                    participant=participant,
                    case_id=case_id,
                    role=data["role"],
                )
                try:
                    turn = SimConversationTurn.objects.create(
                        conversation=conversation,
                        message_id=data["message_id"],
                        player_text=data["question"],
                    )
                except IntegrityError:
                    existing = (
                        SimConversationTurn.objects.select_related("conversation")
                        .get(message_id=data["message_id"])
                    )
                    existing_response, _ = _existing_turn_disposition(
                        existing,
                        participant_id=participant_id,
                        case_id=case_id,
                        role=data["role"],
                        question=data["question"],
                    )
                    return existing_response or _error_response(
                        "idempotency_in_progress",
                        "this message is still being answered",
                        status.HTTP_409_CONFLICT,
                        retry_after_seconds=2,
                    )

            history = _history_from_conversation(conversation)
            upstream_payload = {
                "question": data["question"],
                # Backend persistence is the canonical transcript. Do not trust
                # caller history that could rewrite the agent's memory.
                "history": history,
                "player_id": str(participant_id),
                "message_id": str(data["message_id"]),
                "case_id": case_id,
                # Only explicitly validated, non-adjudicating personas are
                # exposed. Verdicts use the separate contest endpoint.
                "role": data["role"],
            }
            if data["role"] == SimConversation.ROLE_CLERK:
                upstream_payload["contest_state"] = _contest_state(participant, case_id)

            started = time.monotonic()
            upstream = None
            try:
                upstream_attempted = True
                upstream = requests.post(
                    f"{base_url}{ROO_PATH}",
                    headers={
                        "content-type": "application/json",
                        "authorization": f"Bearer {roo_key}",
                    },
                    json=upstream_payload,
                    timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                    stream=True,
                )
            except requests.Timeout:
                logger.warning("Roo sim-patient request timed out")
                return _fail_turn(
                    turn,
                    conversation,
                    started=started,
                    code="timeout",
                    message="simulated patient service timed out",
                    http_status=status.HTTP_504_GATEWAY_TIMEOUT,
                )
            except requests.RequestException:
                logger.exception("Roo sim-patient request failed")
                return _fail_turn(
                    turn,
                    conversation,
                    started=started,
                    code="network_error",
                    message="simulated patient service is unavailable",
                    http_status=status.HTTP_502_BAD_GATEWAY,
                )

            try:
                if not upstream.ok:
                    logger.warning("Roo sim-patient returned HTTP %s", upstream.status_code)
                    return _fail_turn(
                        turn,
                        conversation,
                        started=started,
                        code=f"upstream_{upstream.status_code}",
                        public_code="upstream_error",
                        message="simulated patient service is unavailable",
                        http_status=status.HTTP_502_BAD_GATEWAY,
                    )
                try:
                    payload = _read_limited_json(upstream)
                except requests.RequestException:
                    logger.exception("Roo sim-patient response stream failed")
                    return _fail_turn(
                        turn,
                        conversation,
                        started=started,
                        code="network_error",
                        message="simulated patient service is unavailable",
                        http_status=status.HTTP_502_BAD_GATEWAY,
                    )
            finally:
                try:
                    upstream.close()
                except requests.RequestException:
                    logger.warning("Roo sim-patient response close failed", exc_info=True)

            projected = _project_roo_reply(payload, data["role"], case_id)
            if projected is None:
                logger.warning("Roo sim-patient returned a malformed or oversized response")
                return _fail_turn(
                    turn,
                    conversation,
                    started=started,
                    code="malformed_response",
                    message="simulated patient service returned an invalid response",
                    http_status=status.HTTP_502_BAD_GATEWAY,
                )

            public_response, metadata = projected
            completed_at = timezone.now()
            turn.npc_text = public_response["reply"]
            turn.response_source = metadata["response_source"]
            turn.model_name = metadata["model_name"]
            turn.prompt_tokens = metadata["prompt_tokens"]
            turn.completion_tokens = metadata["completion_tokens"]
            turn.tool_calls = metadata["tool_calls"]
            turn.suggested_action = metadata["suggested_action"]
            turn.latency_ms = max(0, round((time.monotonic() - started) * 1000))
            turn.completed_at = completed_at
            turn.public_response = public_response
            turn.response_status = status.HTTP_200_OK
            turn.save(update_fields=[
                "npc_text",
                "response_source",
                "model_name",
                "prompt_tokens",
                "completion_tokens",
                "tool_calls",
                "suggested_action",
                "latency_ms",
                "completed_at",
                "public_response",
                "response_status",
            ])
            SimConversation.objects.filter(pk=conversation.pk).update(
                last_turn_at=completed_at,
            )
            if budget_reservation is not None:
                if metadata["response_source"] == SimConversationTurn.SOURCE_DETERMINISTIC:
                    budget_reservation.reconcile(0, 0)
                else:
                    budget_reservation.reconcile(
                        metadata["prompt_tokens"],
                        metadata["completion_tokens"],
                    )
            return Response(public_response)
        finally:
            if budget_reservation is not None and budget_reservation.active:
                if upstream_attempted:
                    # A timeout or malformed response may still have incurred
                    # the full model cost, so missing usage stays worst-case.
                    budget_reservation.finalize_unknown()
                else:
                    budget_reservation.cancel()
            lease.release()
