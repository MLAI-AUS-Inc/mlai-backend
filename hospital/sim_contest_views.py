"""
Web ward-game diagnosis contest endpoints.

Flow: the health-hack browser game submits a diagnosis guess to Roo, Roo
adjudicates it deterministically (same fuzzy matcher as the Slack game) and
records the verdict here server-to-server. The first correct RECORDED guess is
atomically assigned the free ticket; later correct guesses receive the 30%
discount. Prize registration only reveals the already-assigned prize.
The registration step stores the email on that guess and returns the assigned
Luma URL directly; it does not send email.

Invariants:
- record/ is service-only (HasStrictRooApiKey) — a browser can never assert "I was
  correct" to this backend.
- One guess per (case_id, client_id): DB unique constraint, get_or_create.
- One winner per case: SimCaseWinner.case_id unique constraint is claimed while
  recording the correct guess; the race loser falls through to the discount.
- Entirely separate from the Slack MedHack tables/gameplay.
"""
import logging
import uuid

import requests
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from core.permissions import HasHealthHackApiKey, HasStrictRooApiKey
from .models import SimCaseWinner, SimDiagnosisGuess, SimParticipant
from .sim_security import LimitedJSONParser, participant_log_id

logger = logging.getLogger(__name__)

ROO_DIAGNOSIS_PATH = '/api/diagnosis-check'
ROO_CONNECT_TIMEOUT_SECONDS = 3
ROO_READ_TIMEOUT_SECONDS = 10


class SimGuessCheckSerializer(serializers.Serializer):
    guess = serializers.CharField(max_length=200, trim_whitespace=True)
    client_id = serializers.UUIDField()


def _participant_for_client(client_id):
    """Return the participant row for a Worker-minted UUID, if applicable."""
    try:
        participant_id = uuid.UUID(str(client_id))
    except (ValueError, TypeError, AttributeError):
        return None
    participant, _ = SimParticipant.objects.get_or_create(id=participant_id)
    # auto_now does not run on get_or_create's existing-row path.
    SimParticipant.objects.filter(pk=participant.pk).update(last_seen_at=timezone.now())
    return participant


def _prize_url(prize_kind):
    if prize_kind == SimDiagnosisGuess.PRIZE_FREE_TICKET:
        return str(getattr(settings, 'HEALTH_HACK_FREE_TICKET_URL', '') or '')
    if prize_kind == SimDiagnosisGuess.PRIZE_DISCOUNT_30:
        return str(getattr(settings, 'HEALTH_HACK_DISCOUNT_URL', '') or '')
    return ''


def _valid_roo_guess_reply(payload):
    return (
        isinstance(payload, dict)
        and payload.get('result') in {
            'correct_first', 'correct_beaten', 'incorrect', 'already_guessed',
        }
        and payload.get('outcome') in {
            'pending_claim', 'incorrect', 'ticket', 'discount',
        }
        and payload.get('prize_kind') in {
            'none', 'free_ticket', 'discount_30',
        }
        and isinstance(payload.get('winner_taken'), bool)
        and isinstance(payload.get('case_id'), int)
        and (
            payload.get('diagnosis') is None
            or isinstance(payload.get('diagnosis'), str)
        )
    )


class SimGuessCheckView(APIView):
    """POST sim-guess/check/ — proxy deterministic adjudication to Roo.

    The browser-facing Worker authenticates here with the Health Hack key. Roo
    remains private, owns the active case and matcher, and records the verdict
    back through the existing service-only record endpoint before responding.
    """

    authentication_classes = []
    permission_classes = [HasHealthHackApiKey]
    parser_classes = [LimitedJSONParser]

    def post(self, request):
        serializer = SimGuessCheckSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        client_id = str(data['client_id'])

        base_url = str(getattr(settings, 'ROO_SERVICE_URL', '') or '').rstrip('/')
        if not base_url:
            return Response(
                {'detail': 'diagnosis service is not configured'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        roo_key = str(getattr(settings, 'ROO_SIM_PATIENT_KEY', '') or '')
        if not roo_key:
            return Response(
                {'detail': 'diagnosis service credential is not configured'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        headers = {
            'content-type': 'application/json',
            'authorization': f'Bearer {roo_key}',
        }

        try:
            upstream = requests.post(
                f'{base_url}{ROO_DIAGNOSIS_PATH}',
                headers=headers,
                json={'guess': data['guess'], 'client_id': client_id},
                timeout=(ROO_CONNECT_TIMEOUT_SECONDS, ROO_READ_TIMEOUT_SECONDS),
            )
        except requests.Timeout:
            logger.warning('Roo diagnosis check timed out')
            return Response(
                {'detail': 'diagnosis service timed out'},
                status=status.HTTP_504_GATEWAY_TIMEOUT,
            )
        except requests.RequestException:
            logger.exception('Roo diagnosis check failed')
            return Response(
                {'detail': 'diagnosis service is unavailable'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if not upstream.ok:
            logger.warning('Roo diagnosis check returned HTTP %s', upstream.status_code)
            return Response(
                {'detail': 'diagnosis service is unavailable'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        try:
            payload = upstream.json()
        except ValueError:
            payload = None

        if not _valid_roo_guess_reply(payload):
            logger.warning('Roo diagnosis check returned a malformed response')
            return Response(
                {'detail': 'diagnosis service returned an invalid response'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        active_case_id = int(getattr(settings, 'HEALTH_HACK_ACTIVE_CASE_ID', 1))
        if payload['case_id'] != active_case_id:
            logger.warning('Roo diagnosis check returned a non-active case')
            return Response(
                {'detail': 'diagnosis service returned an invalid response'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response({
            'result': payload['result'],
            'outcome': payload['outcome'],
            'prize_kind': payload['prize_kind'],
            'winner_taken': payload['winner_taken'],
            'case_id': payload['case_id'],
            'diagnosis': payload['diagnosis'],
        })


class SimGuessClaimThrottle(AnonRateThrottle):
    """Rate limiting for the public claim endpoint."""
    rate = '30/hour'


class SimGuessRecordSerializer(serializers.Serializer):
    case_id = serializers.IntegerField(min_value=1)
    # Optional during rolling deploys so the backend can land before the Roo
    # release that starts sending human-readable challenge titles.
    case_title = serializers.CharField(
        max_length=200,
        trim_whitespace=True,
        allow_blank=True,
        required=False,
        default='',
    )
    client_id = serializers.UUIDField()
    guess_text = serializers.CharField(max_length=300)
    is_correct = serializers.BooleanField()


class SimGuessClaimSerializer(serializers.Serializer):
    case_id = serializers.IntegerField(min_value=1)
    client_id = serializers.UUIDField()
    email = serializers.EmailField(max_length=254)


class SimGuessStatusView(APIView):
    """GET sim-guess/status/ — authoritative one-shot and prize state."""

    authentication_classes = []
    permission_classes = [HasHealthHackApiKey]

    def get(self, request):
        serializer = SimGuessCheckSerializer(data={
            'guess': 'status',
            'client_id': request.query_params.get('client_id', ''),
        })
        serializer.is_valid(raise_exception=True)
        client_id = str(serializer.validated_data['client_id'])
        _participant_for_client(client_id)
        case_id = int(getattr(settings, 'HEALTH_HACK_ACTIVE_CASE_ID', 1))
        guess = SimDiagnosisGuess.objects.filter(
            case_id=case_id,
            client_id=client_id,
        ).first()

        if guess is None:
            return Response({
                'case_id': case_id,
                'state': 'eligible',
                'outcome': None,
                'prize_kind': SimDiagnosisGuess.PRIZE_NONE,
                'redemption_url': None,
            })

        if not guess.is_correct:
            state = 'locked'
        elif guess.outcome == SimDiagnosisGuess.OUTCOME_PENDING_CLAIM:
            state = 'awaiting_claim'
        else:
            state = 'completed'

        return Response({
            'case_id': case_id,
            'state': state,
            'outcome': guess.outcome,
            'prize_kind': guess.prize_kind,
            'redemption_url': (
                _prize_url(guess.prize_kind)
                if state == 'completed'
                else None
            ),
        })


class SimGuessRecordView(APIView):
    """POST sim-guess/record/ — Roo records an adjudicated guess (service-only)."""
    authentication_classes = []
    permission_classes = [HasStrictRooApiKey]
    parser_classes = [LimitedJSONParser]

    def post(self, request):
        serializer = SimGuessRecordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        client_id = str(data['client_id'])
        active_case_id = int(getattr(settings, 'HEALTH_HACK_ACTIVE_CASE_ID', 1))
        if data['case_id'] != active_case_id:
            return Response(
                {'detail': 'case is not active', 'code': 'inactive_case'},
                status=status.HTTP_409_CONFLICT,
            )

        participant = _participant_for_client(client_id)
        with transaction.atomic():
            guess, created = SimDiagnosisGuess.objects.get_or_create(
                case_id=active_case_id,
                client_id=client_id,
                defaults={
                    'participant': participant,
                    'case_title': data['case_title'] or f"Case {active_case_id}",
                    'guess_text': data['guess_text'],
                    'is_correct': data['is_correct'],
                    'outcome': (
                        SimDiagnosisGuess.OUTCOME_PENDING_CLAIM
                        if data['is_correct']
                        else SimDiagnosisGuess.OUTCOME_INCORRECT
                    ),
                    'prize_kind': SimDiagnosisGuess.PRIZE_NONE,
                },
            )

            if created and data['is_correct']:
                try:
                    with transaction.atomic():
                        SimCaseWinner.objects.create(case_id=active_case_id, guess=guess)
                    guess.prize_kind = SimDiagnosisGuess.PRIZE_FREE_TICKET
                except IntegrityError:
                    guess.prize_kind = SimDiagnosisGuess.PRIZE_DISCOUNT_30
                guess.save(update_fields=['prize_kind'])
            elif not created and participant is not None and guess.participant_id is None:
                guess.participant = participant
                guess.save(update_fields=['participant'])

        # On a duplicate, the STORED verdict is authoritative — the new payload
        # is ignored so a re-submission can never upgrade a burnt guess.
        return Response({
            'already_guessed': not created,
            'is_correct': guess.is_correct,
            'outcome': guess.outcome,
            'prize_kind': guess.prize_kind,
            'is_first_solver': guess.prize_kind == SimDiagnosisGuess.PRIZE_FREE_TICKET,
            'winner_taken': SimCaseWinner.objects.filter(case_id=active_case_id).exists(),
        })


class SimGuessClaimView(APIView):
    """POST sim-guess/claim/ — public: save an email against a correct guess.

    Prize kind was assigned atomically when the correct guess was recorded.
    This endpoint saves the email and returns the matching Luma redemption URL.
    No email or other outbound message is sent.
    Idempotent: re-claiming returns the same stored outcome and URL.
    """
    authentication_classes = []
    permission_classes = [HasHealthHackApiKey]
    throttle_classes = [SimGuessClaimThrottle]
    parser_classes = [LimitedJSONParser]

    def post(self, request):
        serializer = SimGuessClaimSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        client_id = str(data['client_id'])
        active_case_id = int(getattr(settings, 'HEALTH_HACK_ACTIVE_CASE_ID', 1))
        if data['case_id'] != active_case_id:
            return Response(
                {'detail': 'case is not active', 'code': 'inactive_case'},
                status=status.HTTP_409_CONFLICT,
            )
        email = data['email'].strip().lower()

        with transaction.atomic():
            try:
                guess = (
                    SimDiagnosisGuess.objects.select_for_update()
                    .get(case_id=active_case_id, client_id=client_id, is_correct=True)
                )
            except SimDiagnosisGuess.DoesNotExist:
                return Response(
                    {'detail': 'nothing to claim'},
                    status=status.HTTP_404_NOT_FOUND,
                )

            if guess.outcome in (SimDiagnosisGuess.OUTCOME_TICKET, SimDiagnosisGuess.OUTCOME_DISCOUNT):
                return Response({
                    'result': guess.outcome,
                    'prize_kind': guess.prize_kind,
                    'redemption_url': _prize_url(guess.prize_kind),
                    'already_claimed': True,
                })

            # Anti double-dip: one prize per email per case (across devices).
            if (
                SimDiagnosisGuess.objects
                .filter(case_id=active_case_id, email__iexact=email)
                .exclude(pk=guess.pk)
                .exists()
            ):
                return Response(
                    {'detail': 'email already used for this case'},
                    status=status.HTTP_409_CONFLICT,
                )

            if guess.prize_kind == SimDiagnosisGuess.PRIZE_FREE_TICKET:
                guess.outcome = SimDiagnosisGuess.OUTCOME_TICKET
            elif guess.prize_kind == SimDiagnosisGuess.PRIZE_DISCOUNT_30:
                guess.outcome = SimDiagnosisGuess.OUTCOME_DISCOUNT
            else:
                logger.error('correct guess %s has no assigned prize', guess.pk)
                return Response(
                    {'detail': 'prize assignment unavailable'},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )

            guess.email = email
            guess.claimed_at = timezone.now()
            try:
                with transaction.atomic():
                    guess.save(update_fields=[
                        'outcome', 'email', 'claimed_at',
                    ])
            except IntegrityError:
                return Response(
                    {'detail': 'email already used for this case'},
                    status=status.HTTP_409_CONFLICT,
                )

        logger.info(
            "sim-guess claim: case=%s participant=%s outcome=%s",
            active_case_id, participant_log_id(client_id), guess.outcome,
        )
        return Response({
            'result': guess.outcome,
            'prize_kind': guess.prize_kind,
            'redemption_url': _prize_url(guess.prize_kind),
            'already_claimed': False,
        })
