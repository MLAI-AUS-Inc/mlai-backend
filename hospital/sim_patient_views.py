"""Authenticated Health Hack gateway to Roo's simulated-patient agent."""

import logging

import requests
from django.conf import settings
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasHealthHackApiKey

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


class SimPatientProxyView(APIView):
    """POST sim-patient/ — securely proxy one player turn to Roo over the VPC."""

    authentication_classes = []
    permission_classes = [HasHealthHackApiKey]

    def post(self, request):
        serializer = SimPatientRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        base_url = str(getattr(settings, 'ROO_SERVICE_URL', '') or '').rstrip('/')
        if not base_url:
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
            'history': data['history'],
            'player_id': data['player_id'],
            # This public gateway only exposes Sash. Other Roo personas remain
            # inaccessible even if a caller adds a role field to its request.
            'role': 'patient',
        }
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
            logger.warning('Roo sim-patient request timed out')
            return Response(
                {'detail': 'simulated patient service timed out'},
                status=status.HTTP_504_GATEWAY_TIMEOUT,
            )
        except requests.RequestException:
            logger.exception('Roo sim-patient request failed')
            return Response(
                {'detail': 'simulated patient service is unavailable'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if not upstream.ok:
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
            logger.warning('Roo sim-patient returned a malformed response')
            return Response(
                {'detail': 'simulated patient service returned an invalid response'},
                status=status.HTTP_502_BAD_GATEWAY,
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
        })
