# app/views.py
import csv
import logging
import datetime
import re
import json
from django.utils import timezone
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.shortcuts import get_object_or_404
from .models import Submission, Team, Announcement
from .serializers import TeamSerializer, SubmissionSerializer, AnnouncementSerializer
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.decorators import api_view, permission_classes
import logging
from dotenv import load_dotenv
from django.db import transaction, IntegrityError
from django.contrib.auth import get_user_model
from rest_framework import permissions, status, generics
from core.permissions import IsLeaderboardAdmin
from rest_framework.response import Response
from rest_framework.views import APIView

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)  

User = get_user_model()
TEAM_CODE_PATTERN = re.compile(r'^TEAM(?P<team_id>\d+)$', re.IGNORECASE)
MEDHACK_TEAM_MIN_MEMBERS = 2
MEDHACK_TEAM_MAX_MEMBERS = 6


def _extract_team_id_from_code(code):
    if code is None:
        return None
    match = TEAM_CODE_PATTERN.match(str(code).strip())
    if not match:
        return None
    try:
        return int(match.group('team_id'))
    except (TypeError, ValueError):
        return None


class TeamListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        teams = Team.objects.all().order_by('team_id')
        member_id = request.query_params.get('member_id')
        if member_id and member_id not in ('undefined', 'null'):
            try:
                member_id = int(member_id)
            except (TypeError, ValueError):
                return Response(
                    {"detail": "member_id must be an integer"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        if member_id and isinstance(member_id, int):
            teams = teams.filter(members__id=member_id).distinct().prefetch_related('members')
            payload = []
            for team in teams:
                payload.append(
                    {
                        "id": team.id,
                        "name": team.team_name,
                        "team_id": team.team_id,
                        "code": f"TEAM{team.team_id}" if team.team_id is not None else None,
                        "avatar_url": team.avatar_url,
                        "members": [
                            {
                                "id": member.id,
                                "full_name": member.full_name,
                                "email": member.email,
                                "avatar_url": member.avatar_url,
                                "role": member.role,
                                "personas": member.personas,
                            }
                            for member in team.members.all()
                        ],
                    }
                )
            return Response(payload, status=status.HTTP_200_OK)

        serializer = TeamSerializer(teams, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request):
        team_name = (request.data.get('team_name') or request.data.get('name') or '').strip()
        requested_code = request.data.get('code')
        requested_avatar_url = request.data.get('avatar_url')

        if not team_name:
            return Response({"error": "team_name is required"}, status=status.HTTP_400_BAD_REQUEST)

        existing_team = Team.objects.filter(team_name__iexact=team_name).first()
        created = False

        if existing_team:
            team = existing_team
            if requested_avatar_url and not team.avatar_url:
                team.avatar_url = requested_avatar_url
                team.save(update_fields=['avatar_url'])
        else:
            create_kwargs = {'team_name': team_name}
            requested_team_id = _extract_team_id_from_code(requested_code)
            if requested_code and requested_team_id is None:
                return Response(
                    {"error": "Invalid team code. Expected format like TEAM12."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if requested_team_id is not None:
                create_kwargs['team_id'] = requested_team_id
            if requested_avatar_url:
                create_kwargs['avatar_url'] = requested_avatar_url

            try:
                team = Team.objects.create(**create_kwargs)
                created = True
            except IntegrityError:
                return Response(
                    {"error": "Team already exists with that ID/code."},
                    status=status.HTTP_409_CONFLICT,
                )

        # Keep one-team-per-user semantics for this hackathon.
        user = request.user
        for current_team in user.hospital_teams.all():
            current_team.members.remove(user)

        if not team.members.filter(id=user.id).exists() and team.members.count() >= MEDHACK_TEAM_MAX_MEMBERS:
            return Response(
                {"error": "Team is full.", "max_members": MEDHACK_TEAM_MAX_MEMBERS},
                status=status.HTTP_409_CONFLICT,
            )

        team.members.add(user)
        if not user.has_team:
            user.has_team = True
            user.save(update_fields=['has_team'])

        serializer = TeamSerializer(team)
        return Response(
            {"created": created, "team": serializer.data},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class JoinTeamView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        team_id = request.data.get('team_id')
        code = request.data.get('code')

        if team_id is None and not code:
            return Response({"error": "team_id or code is required"}, status=status.HTTP_400_BAD_REQUEST)

        lookup_team_id = team_id
        team = None
        if lookup_team_id is None and code:
            lookup_team_id = _extract_team_id_from_code(code)
            if lookup_team_id is not None:
                team = Team.objects.filter(team_id=lookup_team_id).first()
            else:
                # Backward-compatible support where code is actually a team name.
                team = Team.objects.filter(team_name__iexact=str(code).strip()).first()

            if team is None:
                return Response(
                    {"error": "Team not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
        elif lookup_team_id is not None:
            team = get_object_or_404(Team, team_id=lookup_team_id)
        user = request.user

        if not team.members.filter(id=user.id).exists() and team.members.count() >= MEDHACK_TEAM_MAX_MEMBERS:
            return Response(
                {
                    "error": f"Team '{team.team_name}' is full.",
                    "max_members": MEDHACK_TEAM_MAX_MEMBERS,
                },
                status=status.HTTP_409_CONFLICT,
            )

        # Keep one-team-per-user semantics for this hackathon.
        for current_team in user.hospital_teams.all():
            current_team.members.remove(user)

        team.members.add(user)
        if not user.has_team:
            user.has_team = True
            user.save(update_fields=['has_team'])

        return Response(
            {
                "message": f"Joined team {team.team_name}",
                "team_id": team.team_id,
                "team_name": team.team_name,
                "code": f"TEAM{team.team_id}",
                "member_count": team.members.count(),
                "min_members": MEDHACK_TEAM_MIN_MEMBERS,
                "max_members": MEDHACK_TEAM_MAX_MEMBERS,
            },
            status=status.HTTP_200_OK,
        )


class SubmissionListCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        submissions = (
            Submission.objects.filter(user=request.user)
            .select_related('team')
            .order_by('-submitted_at')
        )
        data = [
            {
                "submission_id": sub.id,
                "participant_name": sub.participant_name,
                "score": sub.score,
                "accuracy": sub.accuracy,
                "submitted_at": sub.submitted_at.isoformat(),
                "team": (
                    {
                        "team_id": sub.team.team_id,
                        "team_name": sub.team.team_name,
                        "code": f"TEAM{sub.team.team_id}",
                    }
                    if sub.team
                    else None
                ),
            }
            for sub in submissions
        ]
        return Response(data, status=status.HTTP_200_OK)

    def post(self, request):
        # Reuse existing CSV scoring flow.
        response = submit_predictions(request._request)

        if hasattr(response, 'data'):
            data = response.data
        else:
            try:
                data = json.loads(response.content.decode('utf-8') or '{}')
            except Exception:
                data = {}

        # Normalize error body to the frontend contract.
        if response.status_code >= 400:
            detail = data.get('detail') or data.get('error') or 'Submission failed'
            return JsonResponse({'detail': detail}, status=response.status_code)

        # Contract response shape for /submissions endpoint.
        return JsonResponse(
            {
                'score': data.get('score'),
                'submitted_at': data.get('submitted_at'),
            },
            status=200,
        )


class LeaderboardView(APIView):
    permission_classes = [IsLeaderboardAdmin]

    def get(self, request):
        submissions = (
            Submission.objects.select_related('team')
            .filter(team__isnull=False)
            .order_by('-score', '-submitted_at')
        )

        # Best submission per team.
        best_by_team = {}
        for sub in submissions:
            if sub.team_id not in best_by_team:
                best_by_team[sub.team_id] = sub

        rows = []
        for team_id, sub in best_by_team.items():
            cm = (sub.feedback or {}).get('clinical_metrics', {})
            rows.append(
                {
                    "team_id": sub.team.team_id,
                    "team_name": sub.team.team_name,
                    "score": sub.score,
                    "submitted_at": sub.submitted_at.isoformat(),
                    "patients_saved": cm.get('patients_saved'),
                    "patients_at_risk": cm.get('patients_at_risk'),
                    "crisis_episodes_total": cm.get('crisis_episodes_total'),
                    "false_alarms": cm.get('false_alarms'),
                }
            )

        rows.sort(key=lambda row: row["score"], reverse=True)
        return Response(rows, status=status.HTTP_200_OK)

# Map the original state_label (0-17) to your 4 classes (0-3)
def map_state_label(state_label):
    mapping = {
        0: 0,
        9: 0,
        10: 0,
        1: 1,
        2: 1,
        3: 1,
        4: 1,
        5: 1,
        6: 1,
        7: 1,
        8: 1,
        11: 2,
        12: 2,
        13: 2,
        14: 2,
        15: 2,
        16: 3,
        57: 3, # Handling potential edge case if needed, though not in original
    }
    return mapping.get(int(state_label), -1)

# Module-level cache so the 452K-row CSV is read once per worker, not per request.
_ground_truth_cache = None

def load_ground_truth():
    global _ground_truth_cache
    if _ground_truth_cache is not None:
        return _ground_truth_cache
    gt_rows = []
    try:
        with open('./hospital/solution.csv', 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                gt_rows.append(row)
    except FileNotFoundError:
        logger.error("Ground truth file not found at ./hospital/solution.csv")
        return gt_rows
    _ground_truth_cache = gt_rows
    logger.info(f"Ground truth loaded and cached: {len(gt_rows)} rows")
    return _ground_truth_cache

def custom_score(true_labels, pred_labels):
    """Score predictions using mapped classes: 0=Normal, 1=Warning, 2=Crisis, 3=Other."""
    total_score = 0
    for t, p in zip(true_labels, pred_labels):
        if t == 0:    # Normal
            total_score += 0 if p == 0 else -2
        elif t == 1:  # Warning
            if p == 1:
                total_score += 2
            elif p == 2:
                total_score -= 1
            else:
                total_score -= 3
        elif t == 2:  # Crisis
            if p == 2:
                total_score += 3
            elif p == 1:
                total_score -= 3
            else:
                total_score -= 10
    return total_score


def _error_payload(detail):
    return {
        'detail': detail,
        'error': detail,  # Backward compatibility for older clients.
    }


def _announce_if_top_score(submission):
    """Post a hype message to #medhack-frontiers if this submission is the new #1."""
    try:
        previous_best = (
            Submission.objects
            .exclude(id=submission.id)
            .order_by('-score')
            .values_list('score', flat=True)
            .first()
        )

        if previous_best is not None and submission.score <= previous_best:
            return  # Not a new top score

        from integrations.services.slack import SlackService

        channel_id = SlackService.get_channel_id_by_name("medhack-frontiers")
        if not channel_id:
            logger.warning("Could not find #medhack-frontiers channel for leaderboard announcement")
            return

        team_name = submission.team.team_name if submission.team else "an unnamed team"
        accuracy_pct = round(submission.accuracy * 100, 1)

        # Extract clinical metrics for the announcement
        cm = (submission.feedback or {}).get('clinical_metrics', {})
        saved = cm.get('patients_saved', '?')
        at_risk = cm.get('patients_at_risk', '?')
        false_alarms = cm.get('false_alarms', '?')
        crisis_total = cm.get('crisis_episodes_total', '?')

        import random
        hype_intros = [
            f"STOP EVERYTHING!!! :rotating_light::rotating_light::rotating_light:",
            f"OH MY GOD OH MY GOD OH MY GOD :scream::scream::scream:",
            f"EVERYBODY GET IN HERE RIGHT NOW :mega::mega::mega:",
            f"NO WAYYYYY THIS JUST HAPPENED :exploding_head::exploding_head::exploding_head:",
            f"HOLD THE PHONE!!! DROP WHAT YOU'RE DOING!!! :phone::boom::boom:",
        ]
        hype_bodies = [
            f"*{team_name}* JUST ABSOLUTELY OBLITERATED THE LEADERBOARD WITH A SCORE OF *{submission.score}*!!! ({accuracy_pct}% accuracy) THIS IS NOT A DRILL!!!",
            f"*{team_name}* CAME OUT OF NOWHERE AND DETONATED THE ENTIRE LEADERBOARD!!! *{submission.score} POINTS*!!! ({accuracy_pct}% accuracy) I AM LOSING MY MIND!!!",
            f"*{team_name}* JUST WENT FULL BEAST MODE AND DROPPED A *{submission.score}* ON THE LEADERBOARD!!! ({accuracy_pct}% accuracy) THE CROWD GOES ABSOLUTELY WILD!!!",
            f"*{team_name}* SAID \"HOLD MY COFFEE\" AND CASUALLY POSTED A *{submission.score}*!!! ({accuracy_pct}% accuracy) SOMEONE CALL AN AMBULANCE BECAUSE THE OLD LEADERS ARE FINISHED!!!",
            f"*{team_name}* JUST DID THE UNTHINKABLE!!! *{submission.score} POINTS*!!! ({accuracy_pct}% accuracy) THE LEADERBOARD IS IN SHAMBLES!!! ABSOLUTE CARNAGE!!!",
        ]
        hype_closers = [
            f"Submitted by the legendary *{submission.participant_name}* :crown:\n\nARE YOU GONNA LET THAT STAND?! GET YOUR SUBMISSIONS IN!!! :fire::fire::fire::rocket::rocket::rocket:",
            f"Massive respect to *{submission.participant_name}* :saluting_face:\n\nTHE GAUNTLET HAS BEEN THROWN DOWN!!! WHO'S NEXT?! :boxing_glove::fire::fire:",
            f"*{submission.participant_name}* is BUILT DIFFERENT :sunglasses:\n\nEVERYONE ELSE — YOUR MOVE!!! :chess_pawn::fire::rocket::rocket:",
            f"*{submission.participant_name}* just became PUBLIC ENEMY #1 :dart:\n\nTHE REST OF YOU BETTER START TRAINING HARDER!!! :weight_lifter::fire::fire::fire:",
        ]

        clinical_line = (
            f":hospital: *Clinical Impact:* {saved}/{crisis_total} patients saved | "
            f"{at_risk} lives at risk | {false_alarms} false alarms"
        )

        message = (
            f"{random.choice(hype_intros)}\n\n"
            f":trophy::trophy::trophy: *NEW #1 ON THE LEADERBOARD!!!* :trophy::trophy::trophy:\n\n"
            f"{random.choice(hype_bodies)}\n\n"
            f"{clinical_line}\n\n"
            f"{random.choice(hype_closers)}"
        )

        SlackService.send_message(channel_id, message)
        logger.info(f"Announced new top score {submission.score} by {team_name} in #medhack-frontiers")

    except Exception as e:
        # Never let a Slack failure break the submission flow
        logger.error(f"Failed to announce top score in Slack: {e}")


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def submit_predictions(request):
    if request.method == 'POST':
        if not request.user.is_authenticated:
            return JsonResponse(_error_payload('Authentication required'), status=401)
        logger.info(f"submit_predictions: user={request.user}, is_authenticated={request.user.is_authenticated}")
        
        user = request.user
        participant_name = user.full_name or "Anonymous"

        # Instead of getting team_id from POST, retrieve it from the user.
        # This assumes each user belongs to at least one team. If they might not,
        # you can default to None.
        team = user.hospital_teams.first() if hasattr(user, 'hospital_teams') and user.hospital_teams.exists() else None
        if not team:
            return JsonResponse(
                {
                    **_error_payload('You must join a MedHack team before submitting.'),
                    'min_members': MEDHACK_TEAM_MIN_MEMBERS,
                    'max_members': MEDHACK_TEAM_MAX_MEMBERS,
                },
                status=400,
            )

        team_size = team.members.count()
        if team_size < MEDHACK_TEAM_MIN_MEMBERS or team_size > MEDHACK_TEAM_MAX_MEMBERS:
            return JsonResponse(
                {
                    **_error_payload(f'Team size must be between {MEDHACK_TEAM_MIN_MEMBERS} and {MEDHACK_TEAM_MAX_MEMBERS} members.'),
                    'team_id': team.team_id,
                    'team_name': team.team_name,
                    'member_count': team_size,
                    'min_members': MEDHACK_TEAM_MIN_MEMBERS,
                    'max_members': MEDHACK_TEAM_MAX_MEMBERS,
                },
                status=400,
            )

        csv_file = request.FILES.get('predictions_csv') or request.FILES.get('file')
        if not csv_file:
            return JsonResponse(_error_payload('No CSV file uploaded'), status=400)
        
        # Parse predictions CSV. We assume the CSV has a header like: ID,predicted_label
        pred_labels = []
        file_data = csv_file.read().decode('utf-8').splitlines()
        reader = csv.reader(file_data, delimiter=',')
        header = next(reader, None)  # skip header
        
        try:
            predicted_label_index = header.index('predicted_label')
        except ValueError:
            predicted_label_index = 1  # assume second column
        
        valid_classes = {0, 1, 2, 3}
        for row_num, row in enumerate(reader, start=1):
            if not row:
                continue
            try:
                pred = int(row[predicted_label_index].strip())
            except Exception as e:
                return JsonResponse(_error_payload(f'Error parsing row {row_num}: {str(e)}'), status=400)
            if pred not in valid_classes:
                return JsonResponse(
                    _error_payload(f'Invalid predicted_label "{pred}" at row {row_num}. Must be 0 (Normal), 1 (Warning), 2 (Crisis), or 3 (Other).'),
                    status=400,
                )
            pred_labels.append(pred)
        
        gt_rows_all = load_ground_truth()
        if not gt_rows_all:
             return JsonResponse(_error_payload('Ground truth data not loaded properly'), status=500)
        
        if len(pred_labels) != len(gt_rows_all):
            return JsonResponse({
                **_error_payload(f'Number of predictions ({len(pred_labels)}) does not match number of ground truth rows ({len(gt_rows_all)})')
            }, status=400)
        
        true_labels_all = [int(row['predicted_label']) for row in gt_rows_all]
        score = custom_score(true_labels_all, pred_labels)
        correct_count = sum(1 for t, p in zip(true_labels_all, pred_labels) if t == p)
        accuracy = correct_count / len(true_labels_all) if true_labels_all else 0

        # --- Build compact feedback JSON instead of 180K+ Prediction rows ---
        CLASS_NAMES = {0: 'Normal', 1: 'Warning', 2: 'Crisis', 3: 'Other'}

        # Confusion matrix: confusion[actual][predicted] = count
        confusion = {a: {p: 0 for p in range(4)} for a in range(4)}
        for t, p in zip(true_labels_all, pred_labels):
            if t in confusion and p in confusion[t]:
                confusion[t][p] += 1

        # Per-class stats
        class_stats = {}
        for cls in range(4):
            total = sum(confusion[cls].values())
            correct = confusion[cls][cls]
            class_stats[str(cls)] = {
                'name': CLASS_NAMES[cls],
                'total': total,
                'correct': correct,
                'accuracy': round(correct / total, 4) if total else 0,
            }

        # Missed crises (actual=2, predicted!=2) — first 50
        missed_crises = []
        for i, (t, p) in enumerate(zip(true_labels_all, pred_labels)):
            if t == 2 and p != 2:
                missed_crises.append({
                    'row': i + 1,
                    'predicted': p,
                    'predicted_name': CLASS_NAMES.get(p, 'Unknown'),
                })
                if len(missed_crises) >= 50:
                    break

        # First 100 public rows with detailed feedback
        public_indices = [i for i, row in enumerate(gt_rows_all) if row.get('Usage', '').strip() == 'Public']
        first_100 = []
        for idx in public_indices[:100]:
            t = true_labels_all[idx]
            p = pred_labels[idx]
            first_100.append({
                'row': idx + 1,
                'actual': t,
                'actual_name': CLASS_NAMES.get(t, 'Unknown'),
                'predicted': p,
                'predicted_name': CLASS_NAMES.get(p, 'Unknown'),
                'correct': t == p,
            })

        # --- Clinical metrics: episode-level patient outcomes ---
        # Identify contiguous crisis episodes (blocks where true_label == 2)
        crisis_episodes = []  # list of (start_idx, end_idx) tuples
        in_crisis = False
        ep_start = 0
        for i, t in enumerate(true_labels_all):
            if t == 2 and not in_crisis:
                in_crisis = True
                ep_start = i
            elif t != 2 and in_crisis:
                in_crisis = False
                crisis_episodes.append((ep_start, i))
        if in_crisis:
            crisis_episodes.append((ep_start, len(true_labels_all)))

        # For each episode, check if the model detected at least one crisis row
        patients_saved = 0
        patients_at_risk = 0
        episode_details = []
        for start, end in crisis_episodes:
            detected = any(pred_labels[j] == 2 for j in range(start, end))
            if detected:
                patients_saved += 1
            else:
                patients_at_risk += 1
            episode_details.append({
                'start_row': start + 1,
                'end_row': end,
                'length': end - start,
                'detected': detected,
            })

        # Identify contiguous warning episodes (blocks where true_label == 1)
        warning_episodes = []
        in_warning = False
        for i, t in enumerate(true_labels_all):
            if t == 1 and not in_warning:
                in_warning = True
                ep_start = i
            elif t != 1 and in_warning:
                in_warning = False
                warning_episodes.append((ep_start, i))
        if in_warning:
            warning_episodes.append((ep_start, len(true_labels_all)))

        warnings_caught = sum(
            1 for s, e in warning_episodes
            if any(pred_labels[j] == 1 for j in range(s, e))
        )
        warnings_missed = len(warning_episodes) - warnings_caught

        # False alarms: predicted crisis (2) when actual was not crisis
        false_alarm_rows = [(i, true_labels_all[i]) for i in range(len(true_labels_all))
                           if pred_labels[i] == 2 and true_labels_all[i] != 2]
        false_alarm_breakdown = {0: 0, 1: 0, 3: 0}
        for _, actual in false_alarm_rows:
            if actual in false_alarm_breakdown:
                false_alarm_breakdown[actual] += 1

        clinical_metrics = {
            'crisis_episodes_total': len(crisis_episodes),
            'patients_saved': patients_saved,
            'patients_at_risk': patients_at_risk,
            'crisis_detection_rate': round(patients_saved / len(crisis_episodes), 4) if crisis_episodes else 0,
            'warning_episodes_total': len(warning_episodes),
            'warnings_caught': warnings_caught,
            'warnings_missed': warnings_missed,
            'warning_detection_rate': round(warnings_caught / len(warning_episodes), 4) if warning_episodes else 0,
            'false_alarms': len(false_alarm_rows),
            'false_alarm_breakdown': {
                'actual_normal': false_alarm_breakdown[0],
                'actual_warning': false_alarm_breakdown[1],
                'actual_other': false_alarm_breakdown[3],
            },
            # First 10 missed episodes for debugging
            'missed_episodes_sample': [
                ep for ep in episode_details if not ep['detected']
            ][:10],
        }

        feedback = {
            'confusion_matrix': confusion,
            'class_stats': class_stats,
            'missed_crises': missed_crises,
            'missed_crises_total': sum(1 for t, p in zip(true_labels_all, pred_labels) if t == 2 and p != 2),
            'first_100_public': first_100,
            'clinical_metrics': clinical_metrics,
        }

        # Create Submission with feedback — no Prediction rows needed
        submission = Submission.objects.create(
            user=user,
            team=team,
            participant_name=participant_name,
            score=score,
            accuracy=accuracy,
            feedback=feedback,
        )

        # Disabled: leaderboard is hidden during the competition
        # _announce_if_top_score(submission)

        return JsonResponse({
            'message': 'Submission scored successfully',
            'participant_name': participant_name,
            'team_id': team.team_id if team else None,
            'score': score,
            'accuracy': accuracy,
            'submitted_at': submission.submitted_at.isoformat(),
            'feedback': feedback,
        })
    
    return JsonResponse(_error_payload('Invalid request'), status=405)



@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_submission(request):
    if request.method == 'GET':
        if not request.user.is_authenticated:
            return JsonResponse({'error': 'Authentication required'}, status=401)
        submission = Submission.objects.filter(user=request.user).order_by('-submitted_at').first()
        if not submission:
            return JsonResponse({'error': 'No submission found'}, status=404)

        team_data = None
        if submission.team:
            team_data = {
                'team_id': submission.team.team_id,
                'team_name': submission.team.team_name
            }

        submission_data = {
            'submission_id': submission.id,
            'participant_name': submission.participant_name,
            'score': submission.score,
            'accuracy': submission.accuracy,
            'submitted_at': submission.submitted_at.isoformat(),
            'team': team_data,
            'feedback': submission.feedback,
        }
        return JsonResponse(submission_data)
    return JsonResponse({'error': 'Invalid request'}, status=405)

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_submission_by_id(request, submission_id):
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Authentication required'}, status=401)
    
    try:
        # Ensure the submission belongs to the current user
        submission = Submission.objects.get(id=submission_id, user=request.user)
    except Submission.DoesNotExist:
        return JsonResponse({'error': 'Submission not found'}, status=404)

    team_data = None
    if submission.team:
        team_data = {
            'team_id': submission.team.team_id,
            'team_name': submission.team.team_name
        }

    submission_data = {
        'submission_id': submission.id,
        'participant_name': submission.participant_name,
        'score': submission.score,
        'accuracy': submission.accuracy,
        'submitted_at': submission.submitted_at.isoformat(),
        'team': team_data,
        'feedback': submission.feedback,
    }

    return JsonResponse(submission_data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_recent_submissions(request):
    user = request.user
    team = user.hospital_teams.first() if hasattr(user, 'hospital_teams') else None
    if not team:
        return JsonResponse({'error': 'User is not part of any team'}, status=400)
    
    # Retrieve the 5 most recent submissions for this team
    submissions = Submission.objects.filter(team=team).order_by('-submitted_at')[:5]
    
    submission_list = []
    for sub in submissions:
        submission_list.append({
            'submission_id': sub.id,
            'participant_name': sub.participant_name,
            'score': sub.score,
            'accuracy': sub.accuracy,
            'submitted_at': sub.submitted_at.isoformat(),
            'team': {
                'team_id': team.team_id,
                'team_name': team.team_name
            }
        })
    return JsonResponse(submission_list, safe=False)


class AnnouncementListView(generics.ListAPIView):
    queryset = Announcement.objects.all()
    serializer_class = AnnouncementSerializer
    permission_classes = [permissions.IsAuthenticated]


class ChannelMessagesView(APIView):
    """Read-only feed of messages from the #medhack-frontiers Slack channel."""
    permission_classes = [permissions.IsAuthenticated]

    CHANNEL_NAME = "medhack-frontiers"

    def get(self, request):
        from integrations.services.slack import SlackService

        cursor = request.query_params.get('cursor')
        limit = min(int(request.query_params.get('limit', 30)), 100)

        channel_id = SlackService.get_channel_id_by_name(self.CHANNEL_NAME)
        if not channel_id:
            return Response({"error": f"Channel #{self.CHANNEL_NAME} not found"}, status=status.HTTP_404_NOT_FOUND)

        result = SlackService.get_channel_history(channel_id, limit=limit, cursor=cursor)
        if result is None:
            return Response({"error": "Failed to fetch messages from Slack"}, status=status.HTTP_502_BAD_GATEWAY)

        user_cache = {}
        messages = []
        for msg in result["messages"]:
            user_id = msg.get("user")
            if user_id and user_id not in user_cache:
                user_cache[user_id] = SlackService.get_user_profile(user_id)

            messages.append({
                "ts": msg.get("ts"),
                "text": msg.get("text", ""),
                "user": user_cache.get(user_id),
                "thread_ts": msg.get("thread_ts"),
                "reply_count": msg.get("reply_count", 0),
                "reactions": msg.get("reactions", []),
            })

        return Response({
            "channel": self.CHANNEL_NAME,
            "messages": messages,
            "next_cursor": result.get("next_cursor"),
        })


class ThreadRepliesView(APIView):
    """Read-only replies for a single thread in #medhack-frontiers."""
    permission_classes = [permissions.IsAuthenticated]

    CHANNEL_NAME = "medhack-frontiers"

    def get(self, request, thread_ts):
        from integrations.services.slack import SlackService

        channel_id = SlackService.get_channel_id_by_name(self.CHANNEL_NAME)
        if not channel_id:
            return Response({"error": f"Channel #{self.CHANNEL_NAME} not found"}, status=status.HTTP_404_NOT_FOUND)

        replies = SlackService.get_thread_replies(channel_id, thread_ts)
        if replies is None:
            return Response({"error": "Failed to fetch thread from Slack"}, status=status.HTTP_502_BAD_GATEWAY)

        user_cache = {}
        enriched = []
        for msg in replies:
            user_id = msg.get("user")
            if user_id and user_id not in user_cache:
                user_cache[user_id] = SlackService.get_user_profile(user_id)

            enriched.append({
                "ts": msg.get("ts"),
                "text": msg.get("text", ""),
                "user": user_cache.get(user_id),
                "reactions": msg.get("reactions", []),
            })

        return Response({
            "thread_ts": thread_ts,
            "messages": enriched,
        })
