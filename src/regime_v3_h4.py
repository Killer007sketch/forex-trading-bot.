"""Research only. Causal H4 regime v3, with completed-D1 context and abstention.

Evidence points are NOT probabilities. Reported Wilson lower bounds describe only a
specific six-H4-bar historical outcome proxy, never ground-truth regime accuracy.
No order routing; thresholds are prespecified exploratory choices, NOT optimized.
"""
import csv
import datetime as dt
import hashlib
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

from regime_v2_h4 import WARMUP, classify_v2
from trend_h4 import ema, indicators, load_h4

STATES = ('trend', 'range', 'transition')
HORIZON = 6


def completed_d1_context(bars):
    """D1 direction can first be read after the last H4 candle of a full UTC day."""
    groups = defaultdict(list)
    for i, bar in enumerate(bars):
        groups[bar[0].date()].append(i)
    completed = []
    for _, indices in sorted(groups.items()):
        if len(indices) == 6 and [bars[j][0].hour for j in indices] == [0, 4, 8, 12, 16, 20]:
            completed.append((indices[-1], bars[indices[-1]][4]))
    daily_closes = [close for _, close in completed]
    fast, slow = ema(daily_closes, 20), ema(daily_closes, 50)
    daily_signal = []
    for j, (end_i, close) in enumerate(completed):
        direction = 0
        if j >= 55:
            if fast[j] > slow[j] and fast[j] > fast[j - 5] and close > fast[j]:
                direction = 1
            elif fast[j] < slow[j] and fast[j] < fast[j - 5] and close < fast[j]:
                direction = -1
        daily_signal.append((end_i, direction))
    context = [0] * len(bars)
    last_end, last_direction, cursor = None, 0, 0
    for i in range(len(bars)):
        while cursor < len(daily_signal) and daily_signal[cursor][0] <= i:
            last_end, last_direction = daily_signal[cursor]
            cursor += 1
        if last_end is not None and bars[i][0] - bars[last_end][0] <= dt.timedelta(days=7):
            context[i] = last_direction
    return context


def overlap_and_structure(bars, i, atr):
    left, right = bars[i - 19:i - 9], bars[i - 9:i + 1]
    left_hi, left_lo = max(b[2] for b in left), min(b[3] for b in left)
    right_hi, right_lo = max(b[2] for b in right), min(b[3] for b in right)
    overlap = max(0.0, min(left_hi, right_hi) - max(left_lo, right_lo)) / max(
        min(left_hi - left_lo, right_hi - right_lo), 1e-12)
    up = right_hi > left_hi + .1 * atr and right_lo > left_lo + .1 * atr
    down = right_hi < left_hi - .1 * atr and right_lo < left_lo - .1 * atr
    return min(1.0, overlap), (1 if up else -1 if down else 0)


def classify_v3(bars):
    """Returns regime, direction, evidence count (0..6); all read at H4 close i."""
    base, base_directions = classify_v2(bars)
    fast, slow, atr, adx = indicators(bars)
    d1 = completed_d1_context(bars)
    closes = [b[4] for b in bars]
    labels = ['warmup'] * len(bars)
    directions = [0] * len(bars)
    evidence = [0] * len(bars)
    prev_candidate = ('transition', 0)
    state, held_direction = 'transition', 0
    for i in range(WARMUP, len(bars)):
        if atr[i] is None or atr[i] <= 0 or adx[i] is None:
            continue
        a = atr[i]
        path = sum(abs(closes[j] - closes[j - 1]) for j in range(i - 19, i + 1))
        er = abs(closes[i] - closes[i - 20]) / path if path else 0.0
        slope = (fast[i] - fast[i - 8]) / (8 * a)
        overlap, structure = overlap_and_structure(bars, i, a)
        direction = base_directions[i] if base[i] == 'trend' else 0
        aligned = bool(direction and d1[i] == direction)
        contrary = bool(direction and d1[i] == -direction)
        trend_votes = sum((base[i] == 'trend', structure == direction and direction != 0,
                           aligned, adx[i] >= 22, er >= .25, abs(slope) >= .05))
        range_votes = sum((base[i] == 'range', overlap >= .35, adx[i] <= 25,
                           er <= .38, abs(slope) <= .12, structure == 0))
        # A contrary completed-D1 trend vetoes H4 trend entry; neutral D1 does not.
        trend_entry = (base[i] == 'trend' and direction and not contrary and
                       (aligned or structure == direction) and trend_votes >= 4)
        range_entry = base[i] == 'range' and range_votes >= 5
        candidate = (('trend', direction) if trend_entry else
                     ('range', 0) if range_entry else ('transition', 0))
        if state == 'trend':
            keep = (base[i] == 'trend' and direction == held_direction and not contrary and
                    trend_votes >= 3 and er >= .16 and adx[i] >= 17)
            state, held_direction = ('trend', held_direction) if keep else ('transition', 0)
        elif state == 'range':
            keep = base[i] == 'range' and range_votes >= 4 and overlap >= .2
            state, held_direction = ('range', 0) if keep else ('transition', 0)
        else:
            if candidate != ('transition', 0) and candidate == prev_candidate:
                state, held_direction = candidate
            else:
                state, held_direction = 'transition', 0
        labels[i], directions[i] = state, held_direction
        evidence[i] = trend_votes if state == 'trend' else range_votes if state == 'range' else max(trend_votes, range_votes)
        prev_candidate = candidate
    return labels, directions, evidence


def event_outcome(bars, labels, directions, i, horizon=HORIZON):
    """A precisely defined proxy, not ground-truth market-regime correctness."""
    if labels[i] == 'trend' and directions[i]:
        return directions[i] * (bars[i + horizon][4] - bars[i][4]) > 0
    if labels[i] == 'range':
        high = max(b[2] for b in bars[i - 19:i + 1])
        low = min(b[3] for b in bars[i - 19:i + 1])
        return all(low <= bars[j][4] <= high for j in range(i + 1, i + horizon + 1))
    return None


def wilson_lower(wins, n):
    if n < 30:
        return None
    z = 1.96
    rate = wins / n
    return (rate + z*z/(2*n) - z * math.sqrt(rate*(1-rate)/n + z*z/(4*n*n))) / (1 + z*z/n)


def prequential_confidence(bars, labels, directions, evidence):
    """Use matured, non-overlapping historical events only; never current outcome."""
    counts = defaultdict(lambda: [0, 0])
    estimates = [None] * len(bars)
    sample_sizes = [0] * len(bars)
    for i in range(WARMUP, len(bars)):
        matured = i - HORIZON
        if matured >= WARMUP and (matured - WARMUP) % HORIZON == 0:
            result = event_outcome(bars, labels, directions, matured)
            if result is not None:
                key = (labels[matured], 'high' if evidence[matured] >= 5 else 'standard')
                counts[key][0] += int(result)
                counts[key][1] += 1
        if labels[i] in ('trend', 'range'):
            key = (labels[i], 'high' if evidence[i] >= 5 else 'standard')
            wins, n = counts[key]
            sample_sizes[i] = n
            lower = wilson_lower(wins, n)
            estimates[i] = round(100 * lower, 2) if lower is not None else None
    return estimates, sample_sizes


def report_segment(bars, labels, directions, evidence, confidence, samples, start, end):
    section = labels[start:end]
    counts = Counter(section)
    switches = sum(section[i] != section[i-1] for i in range(1, len(section)))
    result = {'bars': len(section), 'counts': dict(counts),
              'share_pct': {s: round(100 * counts[s] / len(section), 2) for s in STATES},
              'switches': switches, 'switches_per_1000_bars': round(1000*switches/len(section), 2)}
    metrics = {}
    for state in ('trend', 'range'):
        for band in ('all', 'high', 'standard'):
            # Disjoint 6-bar windows avoid spuriously inflated effective sample sizes.
            eligible = [i for i in range(start, end - HORIZON)
                        if (i-WARMUP) % HORIZON == 0 and labels[i] == state and
                        (band == 'all' or (evidence[i] >= 5) == (band == 'high'))]
            outcomes = [event_outcome(bars, labels, directions, i) for i in eligible]
            wins = sum(outcomes)
            lower = wilson_lower(wins, len(outcomes))
            metrics[state + '_' + band] = {'n_nonoverlap': len(outcomes),
                'proxy_success_pct': round(100*wins/len(outcomes), 2) if outcomes else None,
                'wilson_95pct_lower_pct': round(100*lower, 2) if lower is not None else None}
    result['proxy_outcomes'] = metrics
    result['bars_with_prequential_lower_bound'] = sum(confidence[i] is not None for i in range(start, end))
    result['max_past_nonoverlap_samples'] = max(samples[start:end], default=0)
    return result


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python src/regime_v3_h4.py data/eurusd_h1.csv')
    data = Path(sys.argv[1])
    bars = load_h4(data)
    split = int(len(bars) * .7)
    v2_labels, v2_directions = classify_v2(bars)
    v3_labels, v3_directions, evidence = classify_v3(bars)
    v3_confidence, past_samples = prequential_confidence(bars, v3_labels, v3_directions, evidence)
    zeros = [0] * len(bars)
    report = {'data_sha256': hashlib.sha256(data.read_bytes()).hexdigest(),
        'h4_bars': len(bars), 'split_utc': bars[split][0].isoformat(),
        'version': 'v3 fixed thresholds; completed D1 + H4 structure + evidence agreement + abstention',
        'v2': {'development': report_segment(bars, v2_labels, v2_directions, zeros, zeros, zeros, WARMUP, split),
               'historical_holdout': report_segment(bars, v2_labels, v2_directions, zeros, zeros, zeros, split, len(bars))},
        'v3': {'development': report_segment(bars, v3_labels, v3_directions, evidence, v3_confidence, past_samples, WARMUP, split),
               'historical_holdout': report_segment(bars, v3_labels, v3_directions, evidence, v3_confidence, past_samples, split, len(bars))},
        'limitations': 'NO regime truth labels or proven accuracy; proxy trend=direction positive at +6 H4, range=no close outside frozen 20-bar channel by +6 H4. Historical holdout inspected repeatedly; NOT untouched. Fixed thresholds not tuned in this run; samples nonoverlap but serial dependence remains. Online Wilson lower bound is historical proxy reliability, NOT probability of regime correctness. D1 skips partial days; H4 missingness and downloads need audit/pinning. NO trades.'}
    Path('reports').mkdir(exist_ok=True)
    Path('reports/regime_v3_h4.json').write_text(json.dumps(report, indent=2) + '\n')
    with Path('reports/regime_v3_h4.csv').open('w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(['h4_open_utc', 'available_after_h4_close_utc', 'v2_label', 'v3_label', 'v3_direction', 'evidence_votes_not_probability', 'past_proxy_wilson_lower_pct_or_blank', 'past_nonoverlap_n'])
        for i, bar in enumerate(bars):
            writer.writerow([bar[0].isoformat(), (bar[0]+dt.timedelta(hours=4)).isoformat(), v2_labels[i], v3_labels[i], v3_directions[i], evidence[i], v3_confidence[i] if v3_confidence[i] is not None else '', past_samples[i]])
    print(json.dumps(report, indent=2))
    print('RESEARCH ONLY: outcome proxies are not objective regime labels, predictive accuracy, or strategy returns. Live trading disabled.')


if __name__ == '__main__':
    main()
