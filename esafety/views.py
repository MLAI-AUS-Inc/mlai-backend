from rest_framework import status, permissions, generics
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from django.shortcuts import get_object_or_404
from .models import Team, Submission, Announcement, Prediction
from core.models import GlobalSettings
import csv
import logging
import random

logger = logging.getLogger(__name__)

print("DEBUG: ESAFETY VIEWS MODULE LOADED")

from .serializers import TeamSerializer, SubmissionSerializer, AnnouncementSerializer

class TeamListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        teams = Team.objects.all().order_by('team_id')
        serializer = TeamSerializer(teams, many=True)
        return Response(serializer.data)

class TeamNamesListView(APIView):
    permission_classes = [permissions.AllowAny] # Or IsAuthenticated? Prompt says "List available teams". Usually public or auth. Let's assume IsAuthenticated to be safe, or AllowAny if it's for a dropdown on a public page?
    # The prompt says "GET /api/v1/teams/".
    # Let's stick to IsAuthenticated as it's likely for a logged in user updating profile.
    # But wait, "List available teams for the dropdown."
    # If I am registering, I might need it? But profile update implies I am logged in.
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        team_names = Team.objects.values_list('team_name', flat=True)
        return Response({"team_names": list(team_names)})

class JoinTeamView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        team_id = request.data.get('team_id')
        if not team_id:
            return Response({"error": "team_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        team = get_object_or_404(Team, team_id=team_id)
        user = request.user

        # Check if user is already in a team for this hackathon
        if user.esafety_teams.exists():
             # Optional: allow switching teams or enforce one team policy
             # For now, let's assume they can switch or we just add them.
             # If strict one-team policy:
             # return Response({"error": "You are already in a team"}, status=status.HTTP_400_BAD_REQUEST)
             pass

        team.members.add(user)
        return Response({"message": f"Joined team {team.team_name}"}, status=status.HTTP_200_OK)

class SubmissionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        return submit_predictions(request)

LABELS = ['alpha', 'benign', 'bullying', 'conspiracy', 'ed_risk', 'extremist', 'gamergate', 'hate_speech', 'incel_misogyny', 'misinfo', 'pro_ana', 'recovery_ed', 'trad']
TIERS = {
    'benign': {'benign'},
    'recovery': {'recovery_ed'},
    'risky': {'alpha', 'bullying', 'conspiracy', 'ed_risk', 'extremist', 'gamergate', 'hate_speech', 'incel_misogyny', 'misinfo', 'pro_ana', 'trad'}
}

def calculate_f1(true_sets, pred_sets, all_classes):
    f1_scores = []
    for cls in all_classes:
        tp = 0
        fp = 0
        fn = 0
        for t, p in zip(true_sets, pred_sets):
            t_has = cls in t
            p_has = cls in p
            if t_has and p_has:
                tp += 1
            elif p_has and not t_has:
                fp += 1
            elif t_has and not p_has:
                fn += 1
        
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
        f1_scores.append(f1)
    
    return sum(f1_scores) / len(f1_scores) if f1_scores else 0

import json

def load_ground_truth():
    gt_rows = []
    try:
        with open('./esafety/solution_multilabel.csv', 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Filter out Usage column if present in row dict, or just keep it
                # We only need ID and labels
                gt_rows.append(row)
    except FileNotFoundError:
        logger.error("Ground truth file not found at ./esafety/solution_multilabel.csv")
    except Exception as e:
        logger.error(f"Error loading GT CSV: {e}")
    return gt_rows

@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def submit_predictions(request):
    if not request.user.is_authenticated:
        return Response({'error': 'Authentication required'}, status=status.HTTP_401_UNAUTHORIZED)
    
    user = request.user
    participant_name = user.full_name or "Anonymous"
    team = user.esafety_teams.first()
    
    csv_file = request.FILES.get('predictions_csv')
    if not csv_file:
        return Response({'error': 'No CSV file uploaded'}, status=status.HTTP_400_BAD_REQUEST)
    
    # Parse predictions
    try:
        file_data = csv_file.read().decode('utf-8').splitlines()
        reader = csv.DictReader(file_data)
        pred_rows = list(reader)
    except Exception as e:
        return Response({'error': f'Error parsing CSV: {str(e)}'}, status=status.HTTP_400_BAD_REQUEST)
        
    gt_rows = load_ground_truth()
    if not gt_rows:
        return Response({'error': 'Ground truth data not loaded'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    # Validate IDs
    gt_ids = set(row['ID'] for row in gt_rows)
    pred_ids = set(row['ID'] for row in pred_rows)
    
    if not gt_ids.issubset(pred_ids):
        missing = gt_ids - pred_ids
        return Response({'error': f'Missing predictions for IDs: {list(missing)[:5]}...'}, status=status.HTTP_400_BAD_REQUEST)
    
    # Score
    gt_dict = {row['ID']: row for row in gt_rows}
    pred_dict = {row['ID']: row for row in pred_rows}
    
    ids = sorted(gt_dict.keys(), key=lambda x: int(x)) # Use GT IDs to ensure order and subset
    
    true_labels_list = []
    pred_labels_list = []
    true_tiers_list = []
    pred_tiers_list = []
    
    predictions_to_create = []
    
    # Prepare Submission object (unsaved)
    submission = Submission(
        user=user,
        team=team,
        participant_name=participant_name if participant_name else "Anonymous",
        score=0.0,
        coarse_score=0.0,
        fine_score=0.0
    )
    
    for i in ids:
        gt = gt_dict[i]
        pred = pred_dict.get(i)
            
        if not pred:
            # Skip if still not found (should be caught by subset check earlier, but safe to skip)
            continue
        
        # Extract labels
        t_labels = {l for l in LABELS if int(gt.get(l, 0)) == 1}
        p_labels = {l for l in LABELS if int(pred.get(l, 0)) == 1}
        
        true_labels_list.append(t_labels)
        pred_labels_list.append(p_labels)
        
        # Extract tiers
        t_tiers = set()
        p_tiers = set()
        for tier, classes in TIERS.items():
            if not t_labels.isdisjoint(classes):
                t_tiers.add(tier)
            if not p_labels.isdisjoint(classes):
                p_tiers.add(tier)
        
        true_tiers_list.append(t_tiers)
        pred_tiers_list.append(p_tiers)
        
    # Calculate scores
    fine_score = calculate_f1(true_labels_list, pred_labels_list, LABELS)
    coarse_score = calculate_f1(true_tiers_list, pred_tiers_list, ['benign', 'recovery', 'risky'])
    final_score = 0.70 * coarse_score + 0.30 * fine_score
    
    logger.info(f"DEBUG: GT Rows: {len(gt_rows)}, Pred Rows: {len(pred_rows)}, IDs: {len(ids)}")
    
    # Log per-class F1 for fine score
    for cls in LABELS:
        tp = 0
        fp = 0
        fn = 0
        for t, p in zip(true_labels_list, pred_labels_list):
            t_has = cls in t
            p_has = cls in p
            if t_has and p_has:
                tp += 1
            elif p_has and not t_has:
                fp += 1
            elif t_has and not p_has:
                fn += 1
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
        logger.info(f"DEBUG Class {cls}: TP={tp}, FP={fp}, FN={fn}, F1={f1}")

    logger.info(f"Scoring complete. Final: {final_score}, Coarse: {coarse_score}, Fine: {fine_score}")
    logger.info(f"Participant: {submission.participant_name}")
    
    # Save Submission
    submission.score = final_score
    submission.coarse_score = coarse_score
    submission.fine_score = fine_score
    submission.save()
    
    # Save Predictions
    for i, t_labels, p_labels in zip(ids, true_labels_list, pred_labels_list):
        predictions_to_create.append(Prediction(
            submission=submission,
            record_id=i,
            predicted_labels=list(p_labels),
            correct_labels=list(t_labels)
        ))
    
    Prediction.objects.bulk_create(predictions_to_create)
    
    # Construct Team Data
    team_data = None
    if team:
        team_data = {
            "team_id": team.team_id,
            "team_name": team.team_name,
            "team_avatar": team.avatar_url,
            "members": [
                {
                    "full_name": member.full_name,
                    "avatar_url": member.avatar_url
                } for member in team.members.all()
            ]
        }

    # Check obscure setting
    is_obscured = False
    try:
        is_obscured = GlobalSettings.load().is_obscured
    except:
        pass

    return Response({
        'submission_id': submission.id,
        'score': "???" if is_obscured else final_score,
        'coarse_score': "???" if is_obscured else coarse_score,
        'fine_score': "???" if is_obscured else fine_score,
        'submitted_at': "???" if is_obscured else submission.submitted_at,
        'team': team_data
    })

@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_submission(request):
    submission = Submission.objects.filter(user=request.user).order_by('-submitted_at').first()
    if not submission:
        return Response({'detail': 'No submission found.'}, status=status.HTTP_404_NOT_FOUND)
    
    # Check obscure setting
    is_obscured = False
    try:
        is_obscured = GlobalSettings.load().is_obscured
    except:
        pass

    return Response({
        'submission_id': submission.id,
        'score': "???" if is_obscured else submission.score,
        'coarse_score': "???" if is_obscured else submission.coarse_score,
        'fine_score': "???" if is_obscured else submission.fine_score,
        'submitted_at': "???" if is_obscured else submission.submitted_at,
        'team': submission.team.team_name if submission.team else None
    })

class LeaderboardView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        submissions = Submission.objects.all().order_by('-score')
        # Filter to get best score per team/user?
        # For now just all.
        # Check obscure setting
        is_obscured = False
        try:
            is_obscured = GlobalSettings.load().is_obscured
        except:
            pass

        data = []
        for sub in submissions:
            team_data = None
            if sub.team:
                team_data = {
                    "team_id": sub.team.team_id,
                    "team_name": sub.team.team_name,
                    "team_avatar": sub.team.avatar_url,
                    "members": [
                        {
                            "full_name": member.full_name,
                            "avatar_url": member.avatar_url
                        } for member in sub.team.members.all()
                    ]
                }
            
            data.append({
                "id": sub.id,
                "score": "???" if is_obscured else sub.score * 100,
                "accuracy": "???" if is_obscured else sub.fine_score, # Using fine_score (Persona F1) as accuracy
                "submitted_at": "???" if is_obscured else sub.submitted_at,
                "team": team_data,
                "participant_name": sub.participant_name,
                "file_url": sub.file_url,
                "user_avatar": sub.user.avatar_url,
                "user_name": sub.user.full_name
            })
        
        # Randomize order if obscured
        if is_obscured:
            random.shuffle(data)
        
        return Response(data)

class AnnouncementListView(generics.ListAPIView):
    queryset = Announcement.objects.all()
    serializer_class = AnnouncementSerializer
    permission_classes = [permissions.IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        # logger.info(f"AnnouncementListView initial check. User: {request.user}")
        super().initial(request, *args, **kwargs)

