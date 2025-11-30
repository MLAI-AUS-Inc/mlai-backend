import json
import csv
import sys

LABELS = ['alpha', 'benign', 'bullying', 'conspiracy', 'ed_risk', 'extremist', 'gamergate', 'hate_speech', 'incel_misogyny', 'misinfo', 'pro_ana', 'recovery_ed', 'trad']

def calculate_f1(true_sets, pred_sets, all_classes):
    f1_scores = []
    print(f"{'Class':<20} {'TP':<5} {'FP':<5} {'FN':<5} {'Precision':<10} {'Recall':<10} {'F1':<10}")
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
        print(f"{cls:<20} {tp:<5} {fp:<5} {fn:<5} {precision:<10.4f} {recall:<10.4f} {f1:<10.4f}")
    
    return sum(f1_scores) / len(f1_scores) if f1_scores else 0

def load_ground_truth():
    gt_rows = []
    try:
        with open('./esafety/competition_holdout.jsonl', 'r') as f:
            for line in f:
                if not line.strip():
                    continue
                data = json.loads(line)
                row = {'ID': str(data['id'])}
                for label in data.get('category_labels', []):
                    if label in LABELS:
                        row[label] = '1'
                gt_rows.append(row)
    except FileNotFoundError:
        print("Ground truth file not found")
    return gt_rows

def load_predictions():
    pred_rows = []
    try:
        with open('./esafety/sample_submission.csv', 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                pred_rows.append(row)
    except FileNotFoundError:
        print("Prediction file not found")
    return pred_rows

def main():
    gt_rows = load_ground_truth()
    pred_rows = load_predictions()
    
    print(f"Ground Truth Rows: {len(gt_rows)}")
    print(f"Prediction Rows: {len(pred_rows)}")
    
    gt_dict = {row['ID']: row for row in gt_rows}
    pred_dict = {row['ID']: row for row in pred_rows}
    
    gt_ids = set(gt_dict.keys())
    pred_ids = set(pred_dict.keys())
    
    common_ids = gt_ids.intersection(pred_ids)
    print(f"Common IDs: {len(common_ids)}")
    
    if len(common_ids) == 0:
        print("No common IDs found!")
        return

    ids = sorted(list(common_ids))
    
    true_labels_list = []
    pred_labels_list = []
    
    for i in ids:
        gt = gt_dict[i]
        pred = pred_dict[i]
        
        t_labels = {l for l in LABELS if int(gt.get(l, 0)) == 1}
        p_labels = {l for l in LABELS if int(pred.get(l, 0)) == 1}
        
        true_labels_list.append(t_labels)
        pred_labels_list.append(p_labels)
        
    score = calculate_f1(true_labels_list, pred_labels_list, LABELS)
    print(f"\nCalculated Macro F1 Score: {score}")

if __name__ == "__main__":
    main()
