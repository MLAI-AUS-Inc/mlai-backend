import os
import django
import sys
import csv
import json
import logging

sys.path.append('/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mlai.settings')
django.setup()

from esafety.models import Submission
from django.contrib.auth import get_user_model

User = get_user_model()

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

def load_ground_truth():
    gt_rows = []
    try:
        with open('esafety/solution_multilabel.csv', 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Filter out Usage column if present in row dict, or just keep it
                # We only need ID and labels
                gt_rows.append(row)
    except Exception as e:
        print(f"Error loading GT: {e}")
    return gt_rows

def rescore():
    email = "eshinsharma1@gmail.com"
    csv_path = "esafety/submission (6).csv"
    
    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        print(f"User {email} not found. Creating user...")
        user = User.objects.create_user(email=email, password='password123')
        user.first_name = "Eshin"
        user.last_name = "Sharma"
        user.save()
        print(f"Created user {email}")

    submission = Submission.objects.filter(user=user).order_by('-submitted_at').first()
    if not submission:
        print(f"No submission found for {email}. Creating new submission...")
        submission = Submission.objects.create(
            user=user,
            participant_name="Eshin Sharma",
            score=0.0
        )
        print(f"Created submission {submission.id}")

    print(f"Processing submission {submission.id} for {email}...")

    # Load Predictions
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        pred_rows = list(reader)
    
    gt_rows = load_ground_truth()
    
    gt_dict = {row['ID']: row for row in gt_rows}
    pred_dict = {row['ID']: row for row in pred_rows}
    
    ids = sorted(gt_dict.keys(), key=lambda x: int(x))
    
    true_labels_list = []
    pred_labels_list = []
    true_tiers_list = []
    pred_tiers_list = []
    
    for i in ids:
        gt = gt_dict[i]
        pred = pred_dict.get(i) # Direct match, no shift needed
            
        if not pred:
            print(f"Missing prediction for ID {i}")
            continue
            
        t_labels = {l for l in LABELS if int(gt.get(l, 0)) == 1}
        p_labels = {l for l in LABELS if int(pred.get(l, 0)) == 1}
        
        true_labels_list.append(t_labels)
        pred_labels_list.append(p_labels)
        
        t_tiers = set()
        p_tiers = set()
        for tier, classes in TIERS.items():
            if not t_labels.isdisjoint(classes):
                t_tiers.add(tier)
            if not p_labels.isdisjoint(classes):
                p_tiers.add(tier)
                
        true_tiers_list.append(t_tiers)
        pred_tiers_list.append(p_tiers)
        
    fine_score = calculate_f1(true_labels_list, pred_labels_list, LABELS)
    coarse_score = calculate_f1(true_tiers_list, pred_tiers_list, ['benign', 'recovery', 'risky'])
    final_score = 0.70 * coarse_score + 0.30 * fine_score
    
    print(f"New Scores - Final: {final_score}, Coarse: {coarse_score}, Fine: {fine_score}")
    
    submission.score = final_score
    submission.coarse_score = coarse_score
    submission.fine_score = fine_score
    submission.save()
    print("Submission updated successfully.")

if __name__ == '__main__':
    rescore()
