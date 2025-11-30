import json
from collections import Counter

def inspect():
    path = 'esafety/competition_holdout.jsonl'
    total = 0
    usage_counts = Counter()
    label_counts = Counter()
    
    first_few = []
    
    with open(path, 'r') as f:
        for line in f:
            data = json.loads(line)
            total += 1
            usage_counts[data.get('usage', 'unknown')] += 1
            
            for label in data.get('category_labels', []):
                label_counts[label] += 1
                
            if total <= 5:
                first_few.append(data)
                
    print(f"Total Rows: {total}")
    print(f"Usage Counts: {usage_counts}")
    print(f"Label Counts: {label_counts}")
    print("First 5 items:")
    for item in first_few:
        print(f"ID: {item['id']}, Labels: {item.get('category_labels')}, Usage: {item.get('usage')}")

if __name__ == '__main__':
    inspect()
