"""Single held-out label metrics; failed rankings remain unknown, never zero-filled."""

import hashlib
import json
import math


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def metrics(items, target, k):
    if len(items) != len(set(items)) or len(items) > k:
        raise ValueError('duplicate or oversized ranking')
    rank = items.index(target)+1 if target in items else None
    result = {f'ndcg@{k}': 1/math.log2(rank+1) if rank else 0., f'recall@{k}': float(rank is not None)}
    if k >= 10:
        result['ndcg@10'] = 1/math.log2(rank+1) if rank is not None and rank <= 10 else 0.
    if k >= 20:
        result['recall@20'] = float(rank is not None and rank <= 20)
    return result


def aggregate(rows, strategies, k):
    groups = sorted({'all_cases', *(group for row in rows for group in row['groups'])})
    result = {}
    for group in groups:
        subset = [row for row in rows if group == 'all_cases' or group in row['groups']]
        methods = {}
        for strategy in strategies:
            entries = [row['strategies'][strategy] for row in subset]
            succeeded = [entry for entry in entries if entry['status'] == 'completed']
            methods[strategy] = dict(cases=len(entries), evaluated_cases=len(succeeded),
                failed_cases=len(entries)-len(succeeded),
                fallback_cases=sum(entry['fallback_reason'] is not None for entry in succeeded),
                actual_strategy_counts={actual: sum(entry['actual_strategy'] == actual for entry in succeeded)
                                        for actual in sorted({entry['actual_strategy'] for entry in succeeded})},
                metrics={name: sum(entry['metrics'][name] for entry in succeeded)/len(succeeded)
                         if succeeded else None for name in metrics([], '', k)},
                metric_denominator='completed_cases_only_failures_reported_separately')
        result[group] = methods
    return result
