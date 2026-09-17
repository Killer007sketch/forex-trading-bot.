"""Research only. Preserve the original Range/ATR implementation and compare on identical episodes.

Two predeclared exploratory rules: (A) ratio<5 on each of the last three
completed hourly bars; (B) A plus last close at least 10% of the frozen 80h
width inside either boundary. No future observations enter signals. The
2023-26 segment has already been inspected repeatedly and is NOT fresh OOS.
"""
import csv
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

from compare_tpo_atr_h1 import (METHODS, PROFILE_HOURS, collect, pct,
                                range_atr_signal, summarize)
from range_guard_h1 import load_h1
from trend_h4 import indicators, load_h4

FOUR = dt.timedelta(hours=4)
CANDIDATES = ('atr_stable3', 'atr_stable3_interior10')


def variants(prior, low, high):
    """Both variants use only completed bars STRICTLY before decision time."""
    if len(prior) != PROFILE_HOURS or high <= low:
        return (False, False)
    ratios = [range_atr_signal(prior[:len(prior)-offset])
              for offset in (2, 1, 0)]
    if any(value is None for value in ratios):
        return (False, False)
    stable = all(value < 5.0 for value in ratios)
    last_close = prior[-1][4]
    buffer = .10*(high-low)
    interior = low+buffer <= last_close <= high-buffer
    return (stable, stable and interior)


def gate(base, trial):
    """Conservative exploratory admission criteria, fixed before seeing results.

    Baseline remains untouched regardless of gate status. Require >=1 pp
    improvement in balanced accuracy, >=1 pp drop in breakout risk, and
    retain at least 90% of baseline stable-range recall, in EACH segment.
    This gate is NOT an independent prospective validation or deployment gate.
    """
    if not base['flat_predictions'] or not trial['flat_predictions']:
        return False
    return (trial['balanced_proxy_accuracy_pct'] >= base['balanced_proxy_accuracy_pct']+1
            and trial['breakout_risk_among_predicted_flat_pct'] <=
                base['breakout_risk_among_predicted_flat_pct']-1
            and trial['flat_recall_pct'] >= .90*base['flat_recall_pct'])


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python src/atr_stability_experiment_h1.py data/eurusd_h1.csv')
    path = Path(sys.argv[1]); h1 = load_h1(path); h4 = load_h4(path)
    _, _, h4_atr, _ = indicators(h4)
    split = int(len(h4)*.7)
    split_time = h4[split][0]
    end_time = h4[-1][0] + FOUR
    time_index = {row[0]: i for i, row in enumerate(h1)}
    result = {
        'data_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'definition': 'Original ATR20/ATR14<5 unchanged. Variant A: last 3 completed H1 Range/ATR ratios each <5. Variant B: A and prior close inside 10% buffers of frozen 80h extremes. Exactly same daily opportunities and 12h future oracle as compare_tpo_atr_h1.py.',
        'predeclared_research_gate': 'In BOTH historical segments: candidate balanced proxy accuracy at least baseline +1 percentage point; breakout risk among predicted flat at most baseline -1 percentage point; flat recall at least 90% of baseline. Gate only exploratory; no automatic promotion or trading.',
        'segments': {},
        'limitations': 'Repeatedly inspected 2023-26, NOT fresh independent out-of-sample. Future labels are 12h behavior proxies, not ground-truth market state; only midnight samples, H1 OHLC bid-only, no costs/trades. Limited two exploratory variants; selecting based on viewed results can overfit.'
    }
    Path('reports').mkdir(exist_ok=True)
    for name, start, end in (('development', h4[200][0], split_time),
                             ('previously_inspected_2023_2026', split_time, end_time)):
        episodes, audit = collect(h1, h4, h4_atr, start, end)
        if not episodes:
            raise ValueError('No matched episodes')
        for episode in episodes:
            i = time_index[episode['time']]
            past = h1[i-PROFILE_HOURS:i]
            values = variants(past, episode['lower'], episode['upper'])
            episode.update(zip(CANDIDATES, values))
            # Exact baseline is carried from unmodified collect(), never rebuilt.
            assert not episode['combined'] or (episode['range_atr'] and episode['tpo'])
            assert all(not episode[k] or episode['range_atr'] for k in CANDIDATES)
        scored = {method: summarize(episodes, method)
                  for method in ('range_atr', 'tpo', 'combined', *CANDIDATES)}
        baseline = scored['range_atr']
        result['segments'][name] = {
            'audit': audit,
            'always_flat_proxy_prevalence_pct':pct(sum(not x['oracle']['breakout'] for x in episodes),len(episodes)),
            'metrics':scored,
            'exploratory_gates':{candidate:gate(baseline, scored[candidate]) for candidate in CANDIDATES}
        }
        with (Path('reports')/f'atr_stability_{name}.csv').open('w', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['utc','baseline_atr_flat','tpo_flat','combined_flat','stable3_flat',
                             'stable3_interior10_flat','breakout_in_next_12h','ratio_at_decision',
                             'warning_hour_index','confirmation_hour_index'])
            for x in episodes:
                writer.writerow([x['time'].isoformat(),int(x['range_atr']),int(x['tpo']),
                                 int(x['combined']),int(x[CANDIDATES[0]]),int(x[CANDIDATES[1]]),
                                 int(x['oracle']['breakout']),x['ratio'],x['warning'],
                                 x['confirmation'][0] if x['confirmation'] else None])
        print(name, json.dumps(result['segments'][name], sort_keys=True))
    both = {name:all(result['segments'][segment]['exploratory_gates'][name]
                     for segment in result['segments']) for name in CANDIDATES}
    result['passes_both_segments'] = both
    result['baseline_unchanged'] = True
    result['deployed'] = False
    (Path('reports')/'atr_stability_comparison.json').write_text(json.dumps(result,indent=2)+'\n')
    print('RESEARCH ONLY; baseline untouched; passes_both_segments=',json.dumps(both))


if __name__ == '__main__':
    main()
