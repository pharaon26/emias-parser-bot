import json
import os

TARGETS_FILE = os.environ.get('TARGETS_FILE', 'targets.json')


def load_targets():
    if not os.path.exists(TARGETS_FILE):
        return []
    with open(TARGETS_FILE, encoding='utf-8') as f:
        return json.load(f)


def save_targets(targets):
    with open(TARGETS_FILE, 'w', encoding='utf-8') as f:
        json.dump(targets, f, ensure_ascii=False, indent=2)


def add_target(target):
    targets = load_targets()
    targets.append(target)
    save_targets(targets)
    return targets


def remove_target(index):
    targets = load_targets()
    if 0 <= index < len(targets):
        removed = targets.pop(index)
        save_targets(targets)
        return removed
    return None