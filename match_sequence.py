import json
import csv

def match_sequence():
    # Load GT labels
    gt_labels = []
    with open('esafety/competition_holdout.jsonl', 'r') as f:
        for line in f:
            data = json.loads(line)
            lbls = data.get('category_labels', [])
            gt_labels.append(lbls[0] if lbls else 'benign') # Default to benign if empty? Or skip?
            
    print(f"Loaded {len(gt_labels)} GT labels.")
    
    # Load CSV labels
    csv_labels = []
    csv_ids = []
    with open('esafety/submission (6).csv', 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            found = 'benign'
            for k, v in row.items():
                if k != 'ID' and v == '1':
                    found = k
                    break
            csv_labels.append(found)
            csv_ids.append(row['ID'])
            
    print(f"Loaded {len(csv_labels)} CSV labels.")
    
    # Greedy match
    csv_idx = 0
    matches = []
    
    for i, gt_lbl in enumerate(gt_labels):
        # Search for gt_lbl in csv starting at csv_idx
        found_at = -1
        for j in range(csv_idx, len(csv_labels)):
            if csv_labels[j] == gt_lbl:
                found_at = j
                break
        
        if found_at != -1:
            matches.append((i+1, csv_ids[found_at], gt_lbl))
            csv_idx = found_at + 1
        else:
            print(f"Could not find match for GT {i+1} ({gt_lbl}) after CSV index {csv_idx}")
            break
            
        if i < 20:
            print(f"GT {i+1} ({gt_lbl}) -> CSV ID {csv_ids[found_at]} (Index {found_at})")

    print(f"Matched {len(matches)} / {len(gt_labels)} GT items.")

if __name__ == '__main__':
    match_sequence()
