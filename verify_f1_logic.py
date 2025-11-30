
def calculate_f1_score(y_true, y_pred):
    """
    Calculates Macro-averaged F1 score across all label columns.
    y_true and y_pred are lists of dictionaries or lists of lists.
    """
    # Assuming input is list of dicts with 'ID' and labels
    # Extract labels (keys excluding ID)
    if not y_true or not y_pred:
        return 0.0
        
    keys = [k for k in y_true[0].keys() if k != 'ID']
    
    f1_scores = []
    for key in keys:
        tp = 0
        fp = 0
        fn = 0
        for t_row, p_row in zip(y_true, y_pred):
            t_val = t_row[key]
            p_val = p_row[key]
            
            if t_val == 1 and p_val == 1:
                tp += 1
            elif p_val == 1 and t_val == 0:
                fp += 1
            elif t_val == 1 and p_val == 0:
                fn += 1
        
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
        f1_scores.append(f1)
        print(f"Class {key}: TP={tp}, FP={fp}, FN={fn}, F1={f1}")
        
    return sum(f1_scores) / len(f1_scores) if f1_scores else 0

# User's example
# y_true = pd.DataFrame({'ID': [1, 2, 3], 'label_a': [1, 0, 1], 'label_b': [0, 1, 1]})
y_true = [
    {'ID': 1, 'label_a': 1, 'label_b': 0},
    {'ID': 2, 'label_a': 0, 'label_b': 1},
    {'ID': 3, 'label_a': 1, 'label_b': 1}
]

# y_pred = pd.DataFrame({'ID': [1, 2, 3], 'label_a': [1, 0, 0], 'label_b': [0, 1, 1]})
y_pred = [
    {'ID': 1, 'label_a': 1, 'label_b': 0},
    {'ID': 2, 'label_a': 0, 'label_b': 1},
    {'ID': 3, 'label_a': 0, 'label_b': 1}
]

print("Testing User Example:")
score = calculate_f1_score(y_true, y_pred)
print(f"Calculated Score: {score}")
print(f"Expected Score: 0.8333333333333333")

if abs(score - 0.8333333333333333) < 1e-6:
    print("PASS")
else:
    print("FAIL")
