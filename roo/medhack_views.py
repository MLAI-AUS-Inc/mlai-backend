import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasAPIKey, HasRooApiKey
from .models import MedHackCase, MedHackGuess, MedHackWinner
from .serializers import MedHackCaseSerializer, MedHackGuessSerializer, MedHackWinnerSerializer

logger = logging.getLogger(__name__)

MAX_GUESSES_PER_USER = 1


def is_medhack_admin(slack_id: str) -> bool:
    """Check if a Slack user ID is authorized to start MedHack cases."""
    return slack_id in getattr(settings, 'MEDHACK_ADMIN_IDS', [])


class CurrentCaseView(APIView):
    """GET /cases/current/ — Get the active case (404 if none)."""
    authentication_classes = []
    permission_classes = [HasAPIKey | HasRooApiKey]

    def get(self, request):
        case = MedHackCase.objects.filter(is_active=True).first()
        if not case:
            return Response(
                {"error": "No active case"},
                status=status.HTTP_404_NOT_FOUND
            )
        return Response(MedHackCaseSerializer(case).data)


class StartCaseView(APIView):
    """POST /cases/start/ — Start a new case. Closes any active case."""
    authentication_classes = []
    permission_classes = [HasAPIKey | HasRooApiKey]

    def post(self, request):
        case_id = request.data.get('case_id')
        admin_slack_id = request.data.get('admin_slack_id')

        if not case_id or not admin_slack_id:
            return Response(
                {"error": "case_id and admin_slack_id are required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not is_medhack_admin(admin_slack_id):
            return Response(
                {"error": "Only MedHack admins can start cases"},
                status=status.HTTP_403_FORBIDDEN
            )

        with transaction.atomic():
            # Close any currently active case
            MedHackCase.objects.filter(is_active=True).update(
                is_active=False,
                closed_at=timezone.now()
            )

            # Start new case
            case = MedHackCase.objects.create(
                case_id=case_id,
                is_active=True,
                started_by_slack_id=admin_slack_id,
            )

        return Response(MedHackCaseSerializer(case).data, status=status.HTTP_201_CREATED)


class UserCaseStatusView(APIView):
    """GET /cases/active/user/{slack_user_id}/ — User's status for the active case."""
    authentication_classes = []
    permission_classes = [HasAPIKey | HasRooApiKey]

    def get(self, request, slack_user_id):
        case = MedHackCase.objects.filter(is_active=True).first()
        if not case:
            return Response(
                {"error": "No active case"},
                status=status.HTTP_404_NOT_FOUND
            )

        confirmed_guesses = MedHackGuess.objects.filter(
            case=case, slack_user_id=slack_user_id, is_pending=False
        )
        pending_guess = MedHackGuess.objects.filter(
            case=case, slack_user_id=slack_user_id, is_pending=True
        ).first()

        guesses_used = confirmed_guesses.count()
        locked_out = guesses_used >= MAX_GUESSES_PER_USER
        won = MedHackWinner.objects.filter(case=case, slack_user_id=slack_user_id).exists()

        data = {
            "case_id": case.id,
            "yaml_case_id": case.case_id,
            "slack_user_id": slack_user_id,
            "guesses_used": guesses_used,
            "max_guesses": MAX_GUESSES_PER_USER,
            "locked_out": locked_out,
            "won": won,
            "has_pending_guess": pending_guess is not None,
            "pending_guess": pending_guess.guess if pending_guess else None,
        }
        return Response(data)


class PendingGuessView(APIView):
    """
    POST /guesses/pending/ — Store a pending guess.
    DELETE /guesses/pending/ — Clear a pending guess.
    """
    authentication_classes = []
    permission_classes = [HasAPIKey | HasRooApiKey]

    def post(self, request):
        case_id = request.data.get('case_id')
        slack_user_id = request.data.get('slack_user_id')
        guess = request.data.get('guess')

        if not case_id or not slack_user_id or not guess:
            return Response(
                {"error": "case_id, slack_user_id, and guess are required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            case = MedHackCase.objects.get(id=case_id)
        except MedHackCase.DoesNotExist:
            return Response({"error": "Case not found"}, status=status.HTTP_404_NOT_FOUND)

        if not case.is_active:
            return Response(
                {"error": "This case is no longer active"},
                status=status.HTTP_409_CONFLICT
            )

        # Check if user is locked out (already used their guess)
        confirmed_count = MedHackGuess.objects.filter(
            case=case, slack_user_id=slack_user_id, is_pending=False
        ).count()
        if confirmed_count >= MAX_GUESSES_PER_USER:
            return Response(
                {"error": "You have already used your guess for this case"},
                status=status.HTTP_409_CONFLICT
            )

        # Replace any existing pending guess
        MedHackGuess.objects.filter(
            case=case, slack_user_id=slack_user_id, is_pending=True
        ).delete()

        pending = MedHackGuess.objects.create(
            case=case,
            slack_user_id=slack_user_id,
            guess=guess,
            is_pending=True,
        )

        return Response(MedHackGuessSerializer(pending).data, status=status.HTTP_201_CREATED)

    def delete(self, request):
        case_id = request.data.get('case_id')
        slack_user_id = request.data.get('slack_user_id')

        if not case_id or not slack_user_id:
            return Response(
                {"error": "case_id and slack_user_id are required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        deleted, _ = MedHackGuess.objects.filter(
            case_id=case_id, slack_user_id=slack_user_id, is_pending=True
        ).delete()

        if deleted == 0:
            return Response(
                {"error": "No pending guess found"},
                status=status.HTTP_404_NOT_FOUND
            )

        return Response({"status": "cleared"}, status=status.HTTP_200_OK)


class SubmitGuessView(APIView):
    """POST /guesses/submit/ — Submit a confirmed guess."""
    authentication_classes = []
    permission_classes = [HasAPIKey | HasRooApiKey]

    def post(self, request):
        case_id = request.data.get('case_id')
        slack_user_id = request.data.get('slack_user_id')
        guess = request.data.get('guess')
        correct = request.data.get('correct')

        if not case_id or not slack_user_id or guess is None or correct is None:
            return Response(
                {"error": "case_id, slack_user_id, guess, and correct are required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            case = MedHackCase.objects.get(id=case_id)
        except MedHackCase.DoesNotExist:
            return Response({"error": "Case not found"}, status=status.HTTP_404_NOT_FOUND)

        if not case.is_active:
            return Response(
                {"error": "This case is no longer active"},
                status=status.HTTP_409_CONFLICT
            )

        with transaction.atomic():
            # Check if user is locked out
            confirmed_count = MedHackGuess.objects.filter(
                case=case, slack_user_id=slack_user_id, is_pending=False
            ).count()
            if confirmed_count >= MAX_GUESSES_PER_USER:
                return Response(
                    {"error": "You have already used your guess for this case"},
                    status=status.HTTP_409_CONFLICT
                )

            # Clear any pending guess for this user/case
            MedHackGuess.objects.filter(
                case=case, slack_user_id=slack_user_id, is_pending=True
            ).delete()

            # Record the confirmed guess
            confirmed = MedHackGuess.objects.create(
                case=case,
                slack_user_id=slack_user_id,
                guess=guess,
                correct=bool(correct),
                is_pending=False,
                confirmed_at=timezone.now(),
            )

        return Response(MedHackGuessSerializer(confirmed).data, status=status.HTTP_201_CREATED)


class RecordWinnerView(APIView):
    """POST /cases/{case_id}/winners/ — Record a winner."""
    authentication_classes = []
    permission_classes = [HasAPIKey | HasRooApiKey]

    def post(self, request, case_id):
        slack_user_id = request.data.get('slack_user_id')
        is_first_solver = request.data.get('is_first_solver', False)

        if not slack_user_id:
            return Response(
                {"error": "slack_user_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            case = MedHackCase.objects.get(id=case_id)
        except MedHackCase.DoesNotExist:
            return Response({"error": "Case not found"}, status=status.HTTP_404_NOT_FOUND)

        # Check for duplicate winner
        if MedHackWinner.objects.filter(case=case, slack_user_id=slack_user_id).exists():
            return Response(
                {"error": "Winner already recorded for this case"},
                status=status.HTTP_409_CONFLICT
            )

        winner = MedHackWinner.objects.create(
            case=case,
            slack_user_id=slack_user_id,
            is_first_solver=bool(is_first_solver),
        )

        return Response(MedHackWinnerSerializer(winner).data, status=status.HTTP_201_CREATED)


class CaseHistoryView(APIView):
    """GET /cases/history/ — List all played cases with stats."""
    authentication_classes = []
    permission_classes = [HasAPIKey | HasRooApiKey]

    def get(self, request):
        cases = MedHackCase.objects.all().order_by('-started_at')
        data = []
        for case in cases:
            case_data = MedHackCaseSerializer(case).data
            case_data['winners'] = MedHackWinnerSerializer(
                case.winners.all(), many=True
            ).data
            data.append(case_data)
        return Response(data)
