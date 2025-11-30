import json
import csv

def compare_lists():
    # Load GT
    gt_labels = []
    with open('esafety/competition_holdout.jsonl', 'r') as f:
        for line in f:
            data = json.loads(line)
            lbls = data.get('category_labels', [])
            gt_labels.append(lbls[0] if lbls else 'None')
            if len(gt_labels) >= 20:
                break
                
    # Load CSV
    csv_labels = []
    with open('esafety/submission (6).csv', 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            found = 'None'
            for k, v in row.items():
                if k != 'ID' and v == '1':
                    found = k
                    break
            csv_labels.append(found)
            if len(csv_labels) >= 20:
                break
                
    print("GT Labels (IDs 1-20):")
    print(gt_labels)
    print("\nCSV Labels (Rows 0-19):")
    print(csv_labels)

if __name__ == '__main__':
    compare_lists()
