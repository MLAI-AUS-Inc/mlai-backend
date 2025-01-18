# app/views.py
import csv
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from .models import Submission

# Suppose you have your ground_truth.csv with the same number of rows
# or you have ground truth in a database. We'll assume a local CSV for this example.
GROUND_TRUTH_FILE = 'medhack_backend/hospital/Eval_Labels.csv'

def load_ground_truth():
    true_labels = []
    with open(GROUND_TRUTH_FILE, 'r') as f:
        reader = csv.reader(f)
        next(reader)  # skip header if any
        for row in reader:
            # row[0] is the label. Adjust as needed.
            true_labels.append(int(row[0]))
    return true_labels

def custom_score(true_labels, pred_labels):
    normal = {0, 9, 10 }
    warning = {1, 2, 3, 4, 5, 6, 7, 8}
    crisis = {11, 12, 13, 14, 15}

    total_score = 0
    for t, p in zip(true_labels, pred_labels):
        if t in normal:
            if p in normal:
                total_score += 0
            else:
                total_score -= 2
        elif t in warning:
            if p in warning:
                total_score += 2
            elif p in crisis:
                total_score -= 1
            else:
                total_score -= 1
        elif t in crisis:
            if p in crisis:
                total_score += 3
            elif p in warning:
                total_score -= 1
            else:
                total_score -= 2
    return total_score

@csrf_exempt
def submit_predictions(request):
    if request.method == 'POST':
        # Get participant name
        participant_name = request.POST.get('participant_name', 'Anonymous')
        
        # Get the CSV file from the request
        csv_file = request.FILES.get('predictions_csv')
        if not csv_file:
            return JsonResponse({'error': 'No CSV file uploaded'}, status=400)

        # Parse CSV
        pred_labels = []
        file_data = csv_file.read().decode('utf-8').splitlines()
        reader = csv.reader(file_data)
        next(reader)  # skip header if your CSV has one
        for row in reader:
            # assuming row[0] is "predicted_label"
            pred_labels.append(int(row[0]))
        
        # Load ground truth
        true_labels = load_ground_truth()
        
        # Score
        score = custom_score(true_labels, pred_labels)
        
        # Save to DB
        submission = Submission.objects.create(
            participant_name=participant_name,
            score=score
        )
        
        return JsonResponse({
            'message': 'Submission scored successfully',
            'participant_name': participant_name,
            'score': score
        })
    
    return JsonResponse({'error': 'Invalid request'}, status=405)

