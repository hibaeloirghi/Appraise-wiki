import json

# Load the JSON files
with open('/Users/hibaeloirghi/Downloads/Appraise/Examples/PairwiseDocNewUIhibatrial1/old_batches_works.json', 'r') as f1, open('/Users/hibaeloirghi/Downloads/Appraise/Examples/PairwiseDocNewUIhibatrial1/batches.json', 'r') as f2:
    data1 = json.load(f1)
    data2 = json.load(f2)

# Compare the top-level structure of both JSON files
def compare_structure(d1, d2):
    if isinstance(d1, dict) and isinstance(d2, dict):
        return set(d1.keys()) == set(d2.keys())
    elif isinstance(d1, list) and isinstance(d2, list):
        if len(d1) > 0 and len(d2) > 0:
            return compare_structure(d1[0], d2[0])
    return type(d1) == type(d2)

structure_comparison = compare_structure(data1, data2)
print(structure_comparison)

def find_structure_diff(d1, d2, path=""):
    diffs = []
    
    # Check if both are dictionaries
    if isinstance(d1, dict) and isinstance(d2, dict):
        for key in d1.keys() - d2.keys():
            diffs.append(f"{path}/{key} exists in first JSON but not in second")
        for key in d2.keys() - d1.keys():
            diffs.append(f"{path}/{key} exists in second JSON but not in first")
        for key in d1.keys() & d2.keys():
            diffs.extend(find_structure_diff(d1[key], d2[key], f"{path}/{key}"))
    
    # Check if both are lists
    elif isinstance(d1, list) and isinstance(d2, list):
        if len(d1) > 0 and len(d2) > 0:
            diffs.extend(find_structure_diff(d1[0], d2[0], f"{path}/[0]"))
        elif len(d1) != len(d2):
            diffs.append(f"{path} has different list lengths: {len(d1)} vs {len(d2)}")
    
    # Check for type differences
    elif type(d1) != type(d2):
        diffs.append(f"{path} has different types: {type(d1).__name__} vs {type(d2).__name__}")

    return diffs

# Find differences
structure_diffs = find_structure_diff(data1, data2)
structure_diffs[:20]  # Display the first 20 differences for brevity
print(structure_diffs[:20])