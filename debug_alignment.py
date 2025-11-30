import json
import csv

def check_alignment():
    # Load GT IDs
    gt_ids = []
    with open('esafety/competition_holdout.jsonl', 'r') as f:
        for line in f:
            data = json.loads(line)
            gt_ids.append(data['id'])
            
    print(f"GT IDs: Start={gt_ids[0]}, End={gt_ids[-1]}, Count={len(gt_ids)}")
    
    # Load Sample Submission IDs
    pred_ids = []
    with open('esafety/sample_submission.csv', 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            pred_ids.append(int(row['ID']))
            
    print(f"Pred IDs: Start={pred_ids[0]}, End={pred_ids[-1]}, Count={len(pred_ids)}")
    
    # Check overlap
    gt_set = set(gt_ids)
    pred_set = set(pred_ids)
    
    common = gt_set.intersection(pred_set)
    print(f"Common IDs: {len(common)}")
    
    # Check for shift
    # If Pred ID + 1 = GT ID
    shifted_pred = {pid + 1 for pid in pred_ids}
    common_shifted = gt_set.intersection(shifted_pred)
    print(f"Common IDs if Pred+1: {len(common_shifted)}")

if __name__ == '__main__':
    check_alignment()
