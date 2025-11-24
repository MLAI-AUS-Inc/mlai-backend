# app/views.py
import csv
import logging
import datetime
from django.utils import timezone
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from .models import Submission, Team, Prediction
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.decorators import api_view, permission_classes
import logging
from dotenv import load_dotenv
from django.db import transaction
from django.contrib.auth import get_user_model
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)  

User = get_user_model()

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

# Load the full ground truth CSV as a list of dictionaries.
def load_ground_truth():
    gt_rows = []
    # Ensure path is correct relative to where manage.py is run
    try:
        with open('./hospital/test_data_backend.csv', 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                gt_rows.append(row)
    except FileNotFoundError:
        logger.error("Ground truth file not found at ./hospital/test_data_backend.csv")
    return gt_rows

def custom_score(true_labels, pred_labels):
    # (Your custom scoring logic remains unchanged.)
    normal = {0, 9, 10}
    warning = {1, 2, 3, 4, 5, 6, 7, 8}
    crisis = {11, 12, 13, 14, 15}
    total_score = 0
    for t, p in zip(true_labels, pred_labels):
        if t in normal:
            total_score += 0 if p in normal else -2
        elif t in warning:
            if p in warning:
                total_score += 2
            elif p in crisis:
                total_score -= 1
            else:
                total_score -= 3
        elif t in crisis:
            if p in crisis:
                total_score += 3
            elif p in warning:
                total_score -= 3
            else:
                total_score -= 10
    return total_score

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def submit_predictions(request):
    if request.method == 'POST':
        if not request.user.is_authenticated:
            return JsonResponse({'error': 'Authentication required'}, status=401)
        logger.info(f"submit_predictions: user={request.user}, is_authenticated={request.user.is_authenticated}")
        
        user = request.user
        participant_name = user.full_name or "Anonymous"

        # Instead of getting team_id from POST, retrieve it from the user.
        # This assumes each user belongs to at least one team. If they might not,
        # you can default to None.
        team = user.teams.first() if hasattr(user, 'teams') and user.teams.exists() else None

        csv_file = request.FILES.get('predictions_csv')
        if not csv_file:
            return JsonResponse({'error': 'No CSV file uploaded'}, status=400)
        
        # Parse predictions CSV. We assume the CSV has a header like: ID,predicted_label
        pred_labels = []
        file_data = csv_file.read().decode('utf-8').splitlines()
        reader = csv.reader(file_data, delimiter=',')
        header = next(reader, None)  # skip header
        
        try:
            predicted_label_index = header.index('predicted_label')
        except ValueError:
            predicted_label_index = 1  # assume second column
        
        for row in reader:
            if not row:
                continue
            try:
                pred = int(row[predicted_label_index].strip())
                pred_labels.append(pred)
            except Exception as e:
                return JsonResponse({'error': f'Error parsing row {row}: {str(e)}'}, status=400)
        
        gt_rows_all = load_ground_truth()
        if not gt_rows_all:
             return JsonResponse({'error': 'Ground truth data not loaded properly'}, status=500)
        
        if len(pred_labels) != len(gt_rows_all):
            return JsonResponse({
                'error': f'Number of predictions ({len(pred_labels)}) does not match number of ground truth rows ({len(gt_rows_all)})'
            }, status=400)
        
        true_labels_all = [map_state_label(row['state_label']) for row in gt_rows_all]
        score = custom_score(true_labels_all, pred_labels)
        correct_count = sum(1 for t, p in zip(true_labels_all, pred_labels) if t == p)
        accuracy = correct_count / len(true_labels_all) if true_labels_all else 0
        
        # Create a Submission with the authenticated user and associated team (if available)
        submission = Submission.objects.create(
            user=user,
            team=team,
            participant_name=participant_name,
            score=score,
            accuracy=accuracy
        )
        
        public_indices = [i for i, row in enumerate(gt_rows_all) if row.get('Usage', '').strip() == 'Public']

        predictions_to_create = []
        for public_idx, global_idx in enumerate(public_indices, start=1):
            pred = pred_labels[global_idx]
            gt = gt_rows_all[global_idx]
            try:
                ts = datetime.datetime.strptime(gt['timestamp'], '%Y-%m-%d %H:%M:%S')
                # Convert the naive datetime to an aware datetime using the current timezone
                ts = timezone.make_aware(ts, timezone.get_current_timezone())
            except Exception:
                ts = None
            predictions_to_create.append(Prediction(
                submission=submission,
                row_id=public_idx,
                predicted_label=pred,
                correct_label=map_state_label(gt['state_label']),
                timestamp=ts,
                diastolic_bp=float(gt['diastolic_bp']),
                systolic_bp=float(gt['systolic_bp']),
                heart_rate=float(gt['heart_rate']),
                respiratory_rate=float(gt['respiratory_rate']),
                oxygen_saturation=float(gt['oxygen_saturation'])
            ))

        Prediction.objects.bulk_create(predictions_to_create)
        
        return JsonResponse({
            'message': 'Submission scored successfully',
            'participant_name': participant_name,
            'team_id': team.team_id if team else None,
            'score': score,
            'accuracy': accuracy
        })
    
    return JsonResponse({'error': 'Invalid request'}, status=405)



@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_submission(request):
    if request.method == 'GET':
        if not request.user.is_authenticated:
            return JsonResponse({'error': 'Authentication required'}, status=401)
        submission = Submission.objects.filter(user=request.user).order_by('-submitted_at').first()
        if not submission:
            return JsonResponse({'error': 'No submission found'}, status=404)
        
        predictions = submission.predictions.all().order_by('row_id')

        # If the submission has a team, include the team's name in the response
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
            'accuracy': submission.accuracy,  # ensure numeric
            'submitted_at': submission.submitted_at.isoformat(),
            'team': team_data,  # Add the team info
            'predictions': [{
                'row_id': p.row_id,
                'predicted_label': p.predicted_label,
                'correct_label': p.correct_label,
                'timestamp': p.timestamp.isoformat() if p.timestamp else None,
                'diastolic_bp': p.diastolic_bp,
                'systolic_bp': p.systolic_bp,
                'heart_rate': p.heart_rate,
                'respiratory_rate': p.respiratory_rate,
                'oxygen_saturation': p.oxygen_saturation,
            } for p in predictions]
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
    
    predictions = submission.predictions.all().order_by('row_id')
    
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
        'predictions': [{
            'row_id': p.row_id,
            'predicted_label': p.predicted_label,
            'correct_label': p.correct_label,
            'timestamp': p.timestamp.isoformat() if p.timestamp else None,
            'diastolic_bp': p.diastolic_bp,
            'systolic_bp': p.systolic_bp,
            'heart_rate': p.heart_rate,
            'respiratory_rate': p.respiratory_rate,
            'oxygen_saturation': p.oxygen_saturation,
        } for p in predictions]
    }
    
    return JsonResponse(submission_data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_recent_submissions(request):
    user = request.user
    # Assume each user belongs to at least one team.
    # Using the related name "teams" (from the ManyToManyField in Team)
    team = user.teams.first() if hasattr(user, 'teams') else None
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


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_team_names(request):
    # Retrieve all teams ordered by name (or any order you prefer)
    teams = Team.objects.all().order_by('team_name')
    team_names = list(teams.values_list('team_name', flat=True))
    return Response(team_names)
