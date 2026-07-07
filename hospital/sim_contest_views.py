"""
Web ward-game diagnosis contest endpoints.

Flow: the health-hack browser game submits a diagnosis guess to Roo, Roo
adjudicates it deterministically (same fuzzy matcher as the Slack game) and
records the verdict here server-to-server. The player then claims their prize
(free ticket for the first correct email saved, 30% discount for later correct
guessers) directly through the public claim endpoint.

Invariants:
- record/ is service-only (HasRooApiKey) — a browser can never assert "I was
  correct" to this backend.
- One guess per (case_id, client_id): DB unique constraint, get_or_create.
- One winner per case: SimCaseWinner.case_id unique constraint claimed inside
  a transaction + savepoint; the race loser falls through to the discount.
- Entirely separate from the Slack MedHack tables/gameplay.
"""
import logging

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import permissions, serializers, status
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from core.permissions import HasRooApiKey
from .models import SimCaseWinner, SimDiagnosisGuess

logger = logging.getLogger(__name__)


class SimGuessClaimThrottle(AnonRateThrottle):
    """Rate limiting for the public claim endpoint."""
    rate = '30/hour'


class SimGuessRecordSerializer(serializers.Serializer):
    case_id = serializers.IntegerField(min_value=1)
    client_id = serializers.RegexField(regex=r'^[A-Za-z0-9-]{8,64}$', max_length=64)
    guess_text = serializers.CharField(max_length=300)
    is_correct = serializers.BooleanField()


class SimGuessClaimSerializer(serializers.Serializer):
    case_id = serializers.IntegerField(min_value=1)
    client_id = serializers.RegexField(regex=r'^[A-Za-z0-9-]{8,64}$', max_length=64)
    email = serializers.EmailField(max_length=254)


class SimGuessRecordView(APIView):
    """POST sim-guess/record/ — Roo records an adjudicated guess (service-only)."""
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        serializer = SimGuessRecordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        guess, created = SimDiagnosisGuess.objects.get_or_create(
            case_id=data['case_id'],
            client_id=data['client_id'],
            defaults={
                'guess_text': data['guess_text'],
                'is_correct': data['is_correct'],
                'outcome': (
                    SimDiagnosisGuess.OUTCOME_PENDING_CLAIM
                    if data['is_correct']
                    else SimDiagnosisGuess.OUTCOME_INCORRECT
                ),
            },
        )
        # On a duplicate, the STORED verdict is authoritative — the new payload
        # is ignored so a re-submission can never upgrade a burnt guess.
        return Response({
            'already_guessed': not created,
            'is_correct': guess.is_correct,
            'outcome': guess.outcome,
            'winner_taken': SimCaseWinner.objects.filter(case_id=data['case_id']).exists(),
        })


class SimGuessClaimView(APIView):
    """POST sim-guess/claim/ — public: save an email against a correct guess.

    First correct guess to be claimed wins the free ticket (atomic via the
    SimCaseWinner unique constraint); every later correct claim gets the
    30% discount. Idempotent: re-claiming returns the stored outcome.
    """
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [SimGuessClaimThrottle]

    def post(self, request):
        serializer = SimGuessClaimSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        email = data['email'].strip().lower()

        with transaction.atomic():
            try:
                guess = (
                    SimDiagnosisGuess.objects.select_for_update()
                    .get(case_id=data['case_id'], client_id=data['client_id'], is_correct=True)
                )
            except SimDiagnosisGuess.DoesNotExist:
                return Response(
                    {'detail': 'nothing to claim'},
                    status=status.HTTP_404_NOT_FOUND,
                )

            if guess.outcome in (SimDiagnosisGuess.OUTCOME_TICKET, SimDiagnosisGuess.OUTCOME_DISCOUNT):
                return Response({'result': guess.outcome, 'already_claimed': True})

            # Anti double-dip: one prize per email per case (across devices).
            if (
                SimDiagnosisGuess.objects
                .filter(case_id=data['case_id'], email__iexact=email)
                .exclude(pk=guess.pk)
                .exists()
            ):
                return Response(
                    {'detail': 'email already used for this case'},
                    status=status.HTTP_409_CONFLICT,
                )

            try:
                with transaction.atomic():  # savepoint: survive IntegrityError mid-transaction
                    SimCaseWinner.objects.create(case_id=data['case_id'], guess=guess)
                guess.outcome = SimDiagnosisGuess.OUTCOME_TICKET
            except IntegrityError:
                guess.outcome = SimDiagnosisGuess.OUTCOME_DISCOUNT

            guess.email = email
            guess.claimed_at = timezone.now()
            guess.save(update_fields=['outcome', 'email', 'claimed_at'])

        logger.info(
            "sim-guess claim: case=%s client=%s… outcome=%s",
            data['case_id'], data['client_id'][:8], guess.outcome,
        )
        return Response({'result': guess.outcome, 'already_claimed': False})
