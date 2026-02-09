import logging
import re

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.html import escape
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.throttling import AnonRateThrottle

from core.permissions import HasAPIKey, HasRooApiKey
from .models import MedHackCase, MedHackGuess, MedHackWinner

logger = logging.getLogger(__name__)

MAX_GUESSES_PER_USER = 1
MAX_GUESS_LENGTH = 500
SLACK_ID_PATTERN = re.compile(r'^[UW][A-Z0-9]{8,10}$')


class MedHackRateThrottle(AnonRateThrottle):
    """Rate limiting for MedHack endpoints to prevent abuse."""
    rate = '100/hour'


def validate_slack_id(slack_id: str) -> tuple[bool, str]:
    """
    Validate Slack user ID format.
    Returns (is_valid, error_message).
    """
    if not slack_id:
        return False, "slack_user_id is required"
    if slack_id == "system":
        return True, ""
    if not SLACK_ID_PATTERN.match(slack_id):
        return False, "Invalid Slack ID format"
    return True, ""


def sanitize_guess(guess: str) -> tuple[str, str]:
    """
    Sanitize and validate guess input.
    Returns (sanitized_guess, error_message).
    """
    if not guess:
        return "", "guess is required"

    # Strip leading/trailing whitespace
    guess = guess.strip()

    if len(guess) > MAX_GUESS_LENGTH:
        return "", f"guess exceeds maximum length of {MAX_GUESS_LENGTH} characters"

    if len(guess) == 0:
        return "", "guess cannot be empty"

    # Escape HTML to prevent XSS
    sanitized = escape(guess)

    return sanitized, ""


def is_medhack_admin(slack_id: str) -> bool:
    """Check if a Slack user ID is authorized to start MedHack cases."""
    if slack_id == "system":
        return True
    return slack_id in getattr(settings, 'MEDHACK_ADMIN_IDS', [])


class CurrentCaseView(APIView):
    """GET /cases/current/ — Get the active case."""
    authentication_classes = []
    permission_classes = [HasAPIKey | HasRooApiKey]
    throttle_classes = [MedHackRateThrottle]

    def get(self, request):
        case = MedHackCase.objects.filter(is_active=True).first()
        if not case:
            return Response(
                {"detail": "No active case"},
                status=status.HTTP_404_NOT_FOUND
            )
        winner_ids = list(
            case.winners.values_list('slack_user_id', flat=True)
        )
        return Response({
            "case_id": case.case_id,
            "started_at": case.started_at.isoformat(),
            "solved": case.solved,
            "winners": winner_ids,
            "hint_level": case.hint_level,
        })


class StartCaseView(APIView):
    """POST /cases/start/ — Start a new case. Closes any active case."""
    authentication_classes = []
    permission_classes = [HasAPIKey | HasRooApiKey]
    throttle_classes = [MedHackRateThrottle]

    def post(self, request):
        case_id = request.data.get('case_id')
        admin_slack_id = request.data.get('admin_slack_id')

        if case_id is None or not admin_slack_id:
            return Response(
                {"detail": "case_id and admin_slack_id are required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate admin Slack ID
        is_valid, error_msg = validate_slack_id(admin_slack_id)
        if not is_valid:
            return Response(
                {"detail": error_msg},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not is_medhack_admin(admin_slack_id):
            return Response(
                {"detail": "Not authorized to start cases"},
                status=status.HTTP_403_FORBIDDEN
            )

        with transaction.atomic():
            # Close any currently active case
            previous = MedHackCase.objects.filter(is_active=True).first()
            previous_case_id = None
            if previous:
                previous_case_id = previous.case_id
                MedHackCase.objects.filter(is_active=True).update(
                    is_active=False,
                    closed_at=timezone.now()
                )

            # Start new case
            case = MedHackCase.objects.create(
                case_id=int(case_id),
                is_active=True,
                started_by_slack_id=admin_slack_id,
            )

        return Response({
            "case_id": case.case_id,
            "started_at": case.started_at.isoformat(),
            "previous_case_closed": previous_case_id,
        }, status=status.HTTP_201_CREATED)


class UserCaseStatusView(APIView):
    """GET /cases/active/user/{slack_user_id}/ — User's status for the active case."""
    authentication_classes = []
    permission_classes = [HasAPIKey | HasRooApiKey]
    throttle_classes = [MedHackRateThrottle]

    def get(self, request, slack_user_id):
        # Validate Slack ID
        is_valid, error_msg = validate_slack_id(slack_user_id)
        if not is_valid:
            return Response(
                {"detail": error_msg},
                status=status.HTTP_400_BAD_REQUEST
            )
        case = MedHackCase.objects.filter(is_active=True).first()
        if not case:
            return Response(
                {"detail": "No active case"},
                status=status.HTTP_404_NOT_FOUND
            )

        confirmed_count = MedHackGuess.objects.filter(
            case=case, slack_user_id=slack_user_id, is_pending=False
        ).count()

        pending_guess = MedHackGuess.objects.filter(
            case=case, slack_user_id=slack_user_id, is_pending=True
        ).first()

        locked_out = confirmed_count >= MAX_GUESSES_PER_USER
        won = MedHackWinner.objects.filter(case=case, slack_user_id=slack_user_id).exists()

        return Response({
            "case_id": case.case_id,
            "slack_user_id": slack_user_id,
            "guesses_used": confirmed_count,
            "max_guesses": MAX_GUESSES_PER_USER,
            "locked_out": locked_out,
            "won": won,
            "pending_guess": pending_guess.guess if pending_guess else None,
        })


class PendingGuessView(APIView):
    """
    POST /guesses/pending/ — Store a pending guess (upsert).
    DELETE /guesses/pending/ — Clear a pending guess.
    """
    authentication_classes = []
    permission_classes = [HasAPIKey | HasRooApiKey]
    throttle_classes = [MedHackRateThrottle]

    def post(self, request):
        case_id = request.data.get('case_id')
        slack_user_id = request.data.get('slack_user_id')
        guess = request.data.get('guess')

        if case_id is None or not slack_user_id:
            return Response(
                {"detail": "case_id and slack_user_id are required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate Slack ID
        is_valid, error_msg = validate_slack_id(slack_user_id)
        if not is_valid:
            return Response(
                {"detail": error_msg},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Sanitize and validate guess
        sanitized_guess, error_msg = sanitize_guess(guess)
        if error_msg:
            return Response(
                {"detail": error_msg},
                status=status.HTTP_400_BAD_REQUEST
            )

        case = MedHackCase.objects.filter(case_id=int(case_id), is_active=True).first()
        if not case:
            return Response({"detail": "Case not found"}, status=status.HTTP_404_NOT_FOUND)

        # Check if user is locked out
        confirmed_count = MedHackGuess.objects.filter(
            case=case, slack_user_id=slack_user_id, is_pending=False
        ).count()
        if confirmed_count >= MAX_GUESSES_PER_USER:
            return Response(
                {"detail": "User already locked out of this case"},
                status=status.HTTP_409_CONFLICT
            )

        # Upsert: replace any existing pending guess
        MedHackGuess.objects.filter(
            case=case, slack_user_id=slack_user_id, is_pending=True
        ).delete()

        MedHackGuess.objects.create(
            case=case,
            slack_user_id=slack_user_id,
            guess=sanitized_guess,
            is_pending=True,
        )

        return Response({
            "case_id": case.case_id,
            "slack_user_id": slack_user_id,
            "pending_guess": sanitized_guess,
        }, status=status.HTTP_201_CREATED)

    def delete(self, request):
        case_id = request.data.get('case_id')
        slack_user_id = request.data.get('slack_user_id')

        if case_id is None or not slack_user_id:
            return Response(
                {"detail": "case_id and slack_user_id are required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate Slack ID
        is_valid, error_msg = validate_slack_id(slack_user_id)
        if not is_valid:
            return Response(
                {"detail": error_msg},
                status=status.HTTP_400_BAD_REQUEST
            )

        case = MedHackCase.objects.filter(case_id=int(case_id), is_active=True).first()
        if not case:
            return Response(status=status.HTTP_204_NO_CONTENT)

        MedHackGuess.objects.filter(
            case=case, slack_user_id=slack_user_id, is_pending=True
        ).delete()

        return Response(status=status.HTTP_204_NO_CONTENT)


class SubmitGuessView(APIView):
    """POST /guesses/submit/ — Submit a confirmed guess."""
    authentication_classes = []
    permission_classes = [HasAPIKey | HasRooApiKey]
    throttle_classes = [MedHackRateThrottle]

    def post(self, request):
        case_id = request.data.get('case_id')
        slack_user_id = request.data.get('slack_user_id')
        guess = request.data.get('guess')
        correct = request.data.get('correct')

        if case_id is None or not slack_user_id or correct is None:
            return Response(
                {"detail": "case_id, slack_user_id, and correct are required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate Slack ID
        is_valid, error_msg = validate_slack_id(slack_user_id)
        if not is_valid:
            return Response(
                {"detail": error_msg},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Sanitize and validate guess
        sanitized_guess, error_msg = sanitize_guess(guess)
        if error_msg:
            return Response(
                {"detail": error_msg},
                status=status.HTTP_400_BAD_REQUEST
            )

        case = MedHackCase.objects.filter(case_id=int(case_id), is_active=True).first()
        if not case:
            return Response({"detail": "Case not found"}, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic():
            confirmed_count = MedHackGuess.objects.filter(
                case=case, slack_user_id=slack_user_id, is_pending=False
            ).count()
            if confirmed_count >= MAX_GUESSES_PER_USER:
                return Response(
                    {"detail": "User already locked out of this case"},
                    status=status.HTTP_409_CONFLICT
                )

            # Clear any pending guess
            MedHackGuess.objects.filter(
                case=case, slack_user_id=slack_user_id, is_pending=True
            ).delete()

            # Record confirmed guess
            MedHackGuess.objects.create(
                case=case,
                slack_user_id=slack_user_id,
                guess=sanitized_guess,
                correct=bool(correct),
                is_pending=False,
                confirmed_at=timezone.now(),
            )

            new_confirmed_count = confirmed_count + 1

        return Response({
            "case_id": case.case_id,
            "slack_user_id": slack_user_id,
            "guess": sanitized_guess,
            "correct": bool(correct),
            "guesses_used": new_confirmed_count,
            "locked_out": new_confirmed_count >= MAX_GUESSES_PER_USER,
        })


class RecordWinnerView(APIView):
    """POST /cases/{case_id}/winners/ — Record a winner. case_id is the YAML case ID."""
    authentication_classes = []
    permission_classes = [HasAPIKey | HasRooApiKey]
    throttle_classes = [MedHackRateThrottle]

    def post(self, request, case_id):
        slack_user_id = request.data.get('slack_user_id')
        is_first_solver = request.data.get('is_first_solver', False)

        if not slack_user_id:
            return Response(
                {"detail": "slack_user_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate Slack ID
        is_valid, error_msg = validate_slack_id(slack_user_id)
        if not is_valid:
            return Response(
                {"detail": error_msg},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Match against active case's case_id field (YAML ID)
        case = MedHackCase.objects.filter(case_id=int(case_id), is_active=True).first()
        if not case:
            return Response({"detail": "Case not found"}, status=status.HTTP_404_NOT_FOUND)

        if MedHackWinner.objects.filter(case=case, slack_user_id=slack_user_id).exists():
            return Response(
                {"detail": "Winner already recorded for this case"},
                status=status.HTTP_409_CONFLICT
            )

        winner = MedHackWinner.objects.create(
            case=case,
            slack_user_id=slack_user_id,
            is_first_solver=bool(is_first_solver),
        )

        # Mark case as solved
        case.solved = True
        case.save(update_fields=['solved'])

        return Response({
            "case_id": case.case_id,
            "slack_user_id": slack_user_id,
            "is_first_solver": winner.is_first_solver,
            "won_at": winner.won_at.isoformat(),
        }, status=status.HTTP_201_CREATED)


class CaseHistoryView(APIView):
    """GET /cases/history/ — List all played cases with stats."""
    authentication_classes = []
    permission_classes = [HasAPIKey | HasRooApiKey]
    throttle_classes = [MedHackRateThrottle]

    def get(self, request):
        cases = MedHackCase.objects.all().order_by('-started_at')
        data = []
        for case in cases:
            winner_ids = list(
                case.winners.values_list('slack_user_id', flat=True)
            )
            total_guesses = case.guesses.filter(is_pending=False).count()
            data.append({
                "case_id": case.case_id,
                "started_at": case.started_at.isoformat(),
                "closed_at": case.closed_at.isoformat() if case.closed_at else None,
                "solved": case.solved,
                "winners": winner_ids,
                "total_guesses": total_guesses,
            })
        return Response({"cases": data})
