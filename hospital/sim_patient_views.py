"""Authenticated Health Hack gateway to Roo's ward NPC agents."""

import logging
import time
import uuid

import requests
from django.conf import settings
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

logger = logging.getLogger(__name__)

ROO_PATH = '/api/sim-patient'
CONNECT_TIMEOUT_SECONDS = 3
# Gunicorn's production worker timeout is 30 seconds. Leave enough time for
# Django to turn a slow Roo call into a controlled 504 instead of losing the
# worker mid-request.
READ_TIMEOUT_SECONDS = 24
MAX_HISTORY_TURNS = 12


class PatientTurnSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=('player', 'patient'))
    text = serializers.CharField(max_length=1000, trim_whitespace=True)


class SimPatientRequestSerializer(serializers.Serializer):
    question = serializers.CharField(max_length=500, trim_whitespace=True)
    history = PatientTurnSerializer(many=True, required=False, default=list)
    case_id = serializers.IntegerField(min_value=1, required=False)
    player_id = serializers.RegexField(
        regex=r'^[A-Za-z0-9-]{1,64}$',
        max_length=64,
        required=False,
        default='web-anon',
    )
    role = serializers.ChoiceField(
        choices=('patient', 'nurse', 'clerk'),
        required=False,
        default='patient',
    )
    message_id = serializers.UUIDField(required=False)

    def validate_history(self, value):
        return value[-MAX_HISTORY_TURNS:]


def _valid_roo_reply(payload):
    if not isinstance(payload, dict):
        return False
    return (
        isinstance(payload.get('reply'), str)
        and bool(payload['reply'].strip())
        and isinstance(payload.get('case_id'), int)
        and isinstance(payload.get('case_title'), str)
        and isinstance(payload.get('patient_name'), str)
        and isinstance(payload.get('presenting_complaint'), str)
        and isinstance(payload.get('is_guess'), bool)
        and 'correct' in payload
        and payload.get('correct') is None
        and 'diagnosis' in payload
        and payload.get('diagnosis') is None
    )


def _participant_id(player_id):
    """Normalize legacy player ids while preserving Worker-minted UUIDs."""
    try:
        return uuid.UUID(str(player_id))
    except (ValueError, TypeError, AttributeError):
        return uuid.uuid5(uuid.NAMESPACE_URL, f'health-hack:{player_id}')


def _history_from_conversation(conversation):
    """Return the most recent six completed exchanges as 12 wire turns."""
    exchanges = list(
        conversation.turns
        .exclude(response_source=SimConversationTurn.SOURCE_PENDING)
        .exclude(npc_text='')
        .order_by('-created_at')[:6]
    )
    history = []
    for turn in reversed(exchanges):
        history.extend([
            {'role': 'player', 'text': turn.player_text},
            {'role': 'patient', 'text': turn.npc_text},
        ])
    return history


def _contest_state(participant, case_id):
    """Return only the read-only contest context Nurse Paws needs."""
    guess = SimDiagnosisGuess.objects.filter(
        participant=participant,
        case_id=case_id,
    ).first()
    if guess is None:
        return {'state': 'eligible', 'outcome': None}
    if not guess.is_correct:
        state = 'locked'
    elif guess.outcome == SimDiagnosisGuess.OUTCOME_PENDING_CLAIM:
        state = 'awaiting_claim'
    else:
        state = 'completed'
    return {'state': state, 'outcome': guess.outcome}


def _sanitized_tool_calls(payload):
    calls = payload.get('tool_calls')
    if not isinstance(calls, list):
        return []
    sanitized = []
    for call in calls[:8]:
        if not isinstance(call, dict):
            continue
        name = call.get('name')
        arguments = call.get('arguments')
        if not isinstance(name, str) or not name or len(name) > 64:
            continue
        if not isinstance(arguments, dict):
            arguments = {}
        sanitized.append({'name': name, 'arguments': arguments})
    return sanitized


def _sanitized_action(payload, role):
    action = payload.get('suggested_action')
    if role != SimConversation.ROLE_CLERK or not isinstance(action, dict):
        return None
    if action.get('type') != 'confirm_diagnosis':
        return None
    diagnosis = action.get('diagnosis')
    if not isinstance(diagnosis, str):
        return None
    diagnosis = diagnosis.strip()
    if not diagnosis or len(diagnosis) > 200:
        return None
    return {'type': 'confirm_diagnosis', 'diagnosis': diagnosis}


class SimPatientProxyView(APIView):
    """POST sim-patient/ — securely proxy one ward-NPC turn to Roo over the VPC."""

    authentication_classes = []
    permission_classes = [HasHealthHackApiKey]

    def post(self, request):
        serializer = SimPatientRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        participant, _ = SimParticipant.objects.get_or_create(
            id=_participant_id(data['player_id']),
        )
        SimParticipant.objects.filter(pk=participant.pk).update(last_seen_at=timezone.now())
        case_id = data.get(
            'case_id',
            int(getattr(settings, 'HEALTH_HACK_ACTIVE_CASE_ID', 1)),
        )
        conversation, _ = SimConversation.objects.get_or_create(
            participant=participant,
            case_id=case_id,
            role=data['role'],
        )
        history = _history_from_conversation(conversation)
        turn = SimConversationTurn.objects.create(
            conversation=conversation,
            message_id=data.get('message_id') or uuid.uuid4(),
            player_text=data['question'],
        )
        started = time.monotonic()

        def fail_turn(code):
            completed_at = timezone.now()
            turn.response_source = SimConversationTurn.SOURCE_ERROR
            turn.error_code = code
            turn.latency_ms = max(0, round((time.monotonic() - started) * 1000))
            turn.completed_at = completed_at
            turn.save(update_fields=[
                'response_source', 'error_code', 'latency_ms', 'completed_at',
            ])
            SimConversation.objects.filter(pk=conversation.pk).update(
                last_turn_at=completed_at,
            )

        base_url = str(getattr(settings, 'ROO_SERVICE_URL', '') or '').rstrip('/')
        if not base_url:
            fail_turn('not_configured')
            return Response(
                {'detail': 'simulated patient service is not configured'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        headers = {'content-type': 'application/json'}
        roo_key = str(getattr(settings, 'ROO_SIM_PATIENT_KEY', '') or '')
        if roo_key:
            headers['authorization'] = f'Bearer {roo_key}'

        upstream_payload = {
            'question': data['question'],
            # Backend persistence is the canonical transcript. Do not trust a
            # caller-provided history that could rewrite the agent's memory.
            'history': history,
            'player_id': data['player_id'],
            # Only explicitly validated, non-adjudicating personas are exposed.
            # Diagnosis verdicts use the separate contest endpoint.
            'role': data['role'],
        }
        if data['role'] == SimConversation.ROLE_CLERK:
            upstream_payload['contest_state'] = _contest_state(participant, case_id)
        if 'case_id' in data:
            upstream_payload['case_id'] = data['case_id']

        try:
            upstream = requests.post(
                f'{base_url}{ROO_PATH}',
                headers=headers,
                json=upstream_payload,
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
            )
        except requests.Timeout:
            fail_turn('timeout')
            logger.warning('Roo sim-patient request timed out')
            return Response(
                {'detail': 'simulated patient service timed out'},
                status=status.HTTP_504_GATEWAY_TIMEOUT,
            )
        except requests.RequestException:
            fail_turn('network_error')
            logger.exception('Roo sim-patient request failed')
            return Response(
                {'detail': 'simulated patient service is unavailable'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if not upstream.ok:
            fail_turn(f'upstream_{upstream.status_code}')
            logger.warning('Roo sim-patient returned HTTP %s', upstream.status_code)
            return Response(
                {'detail': 'simulated patient service is unavailable'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        try:
            payload = upstream.json()
        except ValueError:
            payload = None

        if not _valid_roo_reply(payload):
            fail_turn('malformed_response')
            logger.warning('Roo sim-patient returned a malformed response')
            return Response(
                {'detail': 'simulated patient service returned an invalid response'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        source = payload.get('response_source')
        if source not in {
            SimConversationTurn.SOURCE_LLM,
            SimConversationTurn.SOURCE_DETERMINISTIC,
        }:
            source = SimConversationTurn.SOURCE_LLM
        usage = payload.get('usage') if isinstance(payload.get('usage'), dict) else {}
        tool_calls = _sanitized_tool_calls(payload)
        suggested_action = _sanitized_action(payload, data['role'])
        turn.npc_text = payload['reply']
        turn.response_source = source
        turn.model_name = str(payload.get('model') or '')[:100]
        turn.prompt_tokens = usage.get('prompt_tokens') if isinstance(usage.get('prompt_tokens'), int) else None
        turn.completion_tokens = usage.get('completion_tokens') if isinstance(usage.get('completion_tokens'), int) else None
        turn.tool_calls = tool_calls
        turn.suggested_action = suggested_action
        turn.latency_ms = max(0, round((time.monotonic() - started) * 1000))
        completed_at = timezone.now()
        turn.completed_at = completed_at
        turn.save(update_fields=[
            'npc_text', 'response_source', 'model_name', 'prompt_tokens',
            'completion_tokens', 'tool_calls', 'suggested_action', 'latency_ms',
            'completed_at',
        ])
        SimConversation.objects.filter(pk=conversation.pk).update(
            last_turn_at=completed_at,
        )

        # Expose only the conversational contract. Roo's envelope currently
        # contains internal case-title and objective presentation text (vitals,
        # investigations, and clinical shorthand) that a player could inspect
        # in DevTools even though the UI never renders it.
        return Response({
            'reply': payload['reply'],
            'case_id': payload['case_id'],
            'case_title': '',
            'patient_name': payload['patient_name'],
            'presenting_complaint': '',
            'is_guess': payload['is_guess'],
            'correct': None,
            'diagnosis': None,
            'suggested_action': suggested_action,
        })
