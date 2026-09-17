"""Research-only causal H4 regime v2 and same-data comparison with unchanged v1.

Labels are known only AFTER their H4 candle closes. No orders are generated.
All thresholds are exploratory, not fitted or validated on an untouched holdout.
"""
import csv
import hashlib
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

from regime_h4 import classify as classify_v1
from trend_h4 import indicators, load_h4

STATES = ('trend', 'range', 'transition')
WARMUP = 220
HORIZON = 6


def efficiency(closes, i, horizon=HORIZON):
    """Absolute net displacement divided by ALL horizon close-to-close moves."""
    path = sum(abs(closes[j] - closes[j - 1]) for j in range(i + 1, i + horizon + 1))
    return abs(closes[i + horizon] - closes[i]) / path if path else 0.0


def decide(state, direction, candidate, candidate_prev, continuation):
    """Entry requires two candidate candles; continuation has looser exit thresholds."""
    if state == 'trend':
        return ('trend', direction) if continuation == ('trend', direction) else ('transition', 0)
    if state == 'range':
        return ('range', 0) if continuation == ('range', 0) else ('transition', 0)
    if candidate != ('transition', 0) and candidate == candidate_prev:
        return candidate
    return 'transition', 0


def classify_v2(bars):
    fast, slow, atr, adx = indicators(bars)
    closes = [bar[4] for bar in bars]
    labels = ['warmup'] * len(bars)
    directions = [0] * len(bars)
    candidate_prev = ('transition', 0)
    state, held_direction = 'transition', 0
    for i in range(WARMUP, len(bars)):
        a, strength = atr[i], adx[i]
        if a is None or a <= 0 or strength is None:
            continue
        path = sum(abs(closes[j] - closes[j - 1]) for j in range(i - 19, i + 1))
        er = abs(closes[i] - closes[i - 20]) / path if path else 0.0
        slope = (fast[i] - fast[i - 8]) / (8 * a)
        prior_high = max(bar[2] for bar in bars[i - 20:i])
        prior_low = min(bar[3] for bar in bars[i - 20:i])
        width = (prior_high - prior_low) / a
        direction = 1 if slope > 0 and fast[i] > slow[i] else -1 if slope < 0 and fast[i] < slow[i] else 0
        breakout = (direction == 1 and closes[i] > prior_high) or (direction == -1 and closes[i] < prior_low)
        trend_entry = (direction != 0 and strength >= 23 and er >= 0.28
                       and abs(slope) >= 0.065 and (breakout or (strength >= 28 and er >= 0.38)))
        range_entry = (strength <= 22 and er <= 0.32 and abs(slope) <= 0.08
                       and width <= 9 and prior_low <= closes[i] <= prior_high)
        candidate = ('trend', direction) if trend_entry else ('range', 0) if range_entry else ('transition', 0)
        trend_keep = (state == 'trend' and direction == held_direction and strength >= 18
                      and er >= 0.18 and abs(slope) >= 0.025
                      and held_direction * (closes[i] - fast[i]) >= -0.5 * a)
        range_keep = (state == 'range' and strength <= 27 and er <= 0.45
                      and abs(slope) <= 0.14 and prior_low - 0.2 * a <= closes[i] <= prior_high + 0.2 * a)
        continuation = ('trend', held_direction) if trend_keep else ('range', 0) if range_keep else ('transition', 0)
        state, held_direction = decide(state, held_direction, candidate, candidate_prev, continuation)
        labels[i], directions[i] = state, held_direction
        candidate_prev = candidate
    return labels, directions


def diagnostic(bars, labels, directions, start, end):
    closes = [b[4] for b in bars]
    _, _, atr, _ = indicators(bars)
    section = labels[start:end]
    counts = Counter(section)
    switches = sum(section[j] != section[j - 1] for j in range(1, len(section)))
    runs = {state: [] for state in STATES}
    if section:
        state, length = section[0], 1
        for label in section[1:]:
            if label == state:
                length += 1
            else:
                if state in runs:
                    runs[state].append(length)
                state, length = label, 1
        if state in runs:
            runs[state].append(length)
    forward_efficiency = {state: [] for state in STATES}
    trend_continuation = []
    range_breakouts = []
    # The future horizon must stay wholly INSIDE this evaluation segment.
    for i in range(start, end - HORIZON):
        state = labels[i]
        if state not in forward_efficiency:
            continue
        forward_efficiency[state].append(efficiency(closes, i))
        if state == 'trend' and directions[i]:
            trend_continuation.append(directions[i] * (closes[i + HORIZON] - closes[i]) > 0)
        if state == 'range':
            hi = max(b[2] for b in bars[i - 19:i + 1])
            lo = min(b[3] for b in bars[i - 19:i + 1])
            range_breakouts.append(any(closes[j] > hi or closes[j] < lo for j in range(i + 1, i + HORIZON + 1)))
    total = len(section)
    return {
        'bars': total,
        'counts': {k: counts[k] for k in STATES},
        'share_pct': {k: round(100 * counts[k] / total, 2) for k in STATES},
        'switches': switches,
        'switches_per_1000_bars': round(1000 * switches / total, 2),
        'median_run_bars': {k: statistics.median(runs[k]) if runs[k] else None for k in STATES},
        'mean_forward_6bar_efficiency': {k: round(statistics.mean(forward_efficiency[k]), 4) if forward_efficiency[k] else None for k in STATES},
        'forward_samples': {k: len(forward_efficiency[k]) for k in STATES},
        'trend_direction_continuation_pct': round(100 * statistics.mean(trend_continuation), 2) if trend_continuation else None,
        'trend_direction_samples': len(trend_continuation),
        'range_breakout_next_6bar_pct': round(100 * statistics.mean(range_breakouts), 2) if range_breakouts else None,
        'range_breakout_samples': len(range_breakouts),
    }


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python src/regime_v2_h4.py data/eurusd_h1.csv')
    data = Path(sys.argv[1])
    bars = load_h4(data)
    split = int(len(bars) * 0.7)
    v1_labels = classify_v1(bars)
    fast, _, _, _ = indicators(bars)
    v1_directions = [0] * len(bars)
    for i in range(WARMUP, len(bars)):
        if v1_labels[i] == 'trend':
            v1_directions[i] = 1 if fast[i] > fast[i - 8] else -1
    v2_labels, v2_directions = classify_v2(bars)
    report = {
        'data_sha256': hashlib.sha256(data.read_bytes()).hexdigest(),
        'h4_bars': len(bars),
        'split_utc': bars[split][0].isoformat(),
        'method': 'v1 unchanged versus v2 two-candle entry and looser state-specific continuation; fixed exploratory thresholds',
        'v1': {'development': diagnostic(bars, v1_labels, v1_directions, WARMUP, split),
               'holdout': diagnostic(bars, v1_labels, v1_directions, split, len(bars))},
        'v2': {'development': diagnostic(bars, v2_labels, v2_directions, WARMUP, split),
               'holdout': diagnostic(bars, v2_labels, v2_directions, split, len(bars))},
        'limitations': 'No regime ground truth, no profitability test; overlapping 6-bar forward observations; previously inspected holdout is NOT untouched. Data re-downloaded unless pinned.'
    }
    Path('reports').mkdir(exist_ok=True)
    Path('reports/regime_v2_h4.json').write_text(json.dumps(report, indent=2) + '\n')
    with Path('reports/regime_v2_h4.csv').open('w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(['h4_open_utc', 'label_available_after_close_v1', 'label_available_after_close_v2', 'v2_direction'])
        writer.writerows((bar[0].isoformat(), v1_labels[i], v2_labels[i], v2_directions[i]) for i, bar in enumerate(bars))
    print(json.dumps(report, indent=2))
    print('RESEARCH ONLY. Labels use completed bars; measurements are descriptive proxies, NOT classification accuracy or verified trading returns.')


if __name__ == '__main__':
    main()
