"""Research only: three flat filters on EXACTLY the same daily EURUSD H1 episodes.

No orders. The future label is a defined price-behaviour proxy, NOT human-labelled
flat/trend ground truth. Both classifiers see ONLY the 80 H1 hours strictly before
00:00 UTC; H4 ATR is taken exclusively from completed H4 candles. Both use the
same frozen 80-hour outer-channel bounds for evaluation. Daily sampling keeps
12-hour target windows disjoint; incomplete price data/targets are censored.
"""
import bisect
import csv
import datetime as dt
import hashlib
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

from range_guard_h1 import load_h1
from tpo_range_h1 import PROFILE_HOURS, profile
from trend_h4 import indicators, load_h4

HOUR = dt.timedelta(hours=1)
FOUR = dt.timedelta(hours=4)
LOOKBACK = 20
ATR_PERIOD = 14
RATIO_THRESHOLD = 5.0  # User-supplied prototype, not optimized.
TPO_MAX_VA_H4_ATR = 4.0  # Original stricter prototype, not retuned.
HORIZON = 12
EXTENSION = 0.05
METHODS = ('tpo', 'range_atr', 'combined')


def contiguous(bars, initial):
    return bool(bars) and bars[0][0] == initial and all(
        b[0] - a[0] == HOUR for a, b in zip(bars, bars[1:]))


def simple_atr(bars, length=ATR_PERIOD):
    """Trailing SMA of true range, requires previous close, all known at t."""
    if len(bars) < length + 1 or not contiguous(bars[-(length+1):], bars[-(length+1)][0]):
        return None
    block = bars[-(length+1):]
    true_ranges = [max(bar[2] - bar[3], abs(bar[2] - block[i-1][4]),
                       abs(bar[3] - block[i-1][4])) for i, bar in enumerate(block) if i]
    return sum(true_ranges) / length


def range_atr_signal(bars, threshold=RATIO_THRESHOLD):
    """The provided ACSIL formula, computed on fully closed trailing H1 only."""
    if len(bars) < max(LOOKBACK, ATR_PERIOD+1):
        return None
    atr = simple_atr(bars)
    if atr is None or atr <= 0:
        return None
    part = bars[-LOOKBACK:]
    return (max(b[2] for b in part) - min(b[3] for b in part)) / atr


def future_oracle(bars, lower, upper):
    """First 3 SAME-SIDE adjacent closes outside frozen channel, final >=5% width.

    Returns the first exterior close and third-close positions for delay scoring.
    Only AFTER-the-fact labels; never passed to any classifier.
    """
    margin = EXTENSION * (upper-lower)
    side = streak = 0
    first = None
    for j, bar in enumerate(bars):
        direction = 1 if bar[4] > upper else -1 if bar[4] < lower else 0
        if direction == 0:
            side = streak = 0
            first = None
        elif direction != side:
            side, streak, first = direction, 1, j
        else:
            streak += 1
        if streak >= 3 and ((bar[4]-upper >= margin) if side == 1 else (lower-bar[4] >= margin)):
            return {'breakout': True, 'side': side, 'first': first, 'third': j}
    return {'breakout': False, 'side': 0, 'first': None, 'third': None}


def alerts(bars, lower, upper):
    """First wick warning and first 2-close confirmation within 12 future H1.

    These prices become observable at each candle CLOSE; no intrabar order assumed.
    """
    warning = confirmation = None
    prev_side = 0
    first_outside_close = None
    for j, bar in enumerate(bars):
        if warning is None and (bar[2] > upper or bar[3] < lower):
            warning = j
        side = 1 if bar[4] > upper else -1 if bar[4] < lower else 0
        if side and side == prev_side and confirmation is None:
            confirmation = (j, side, first_outside_close)
        first_outside_close = j if side and side != prev_side else (first_outside_close if side else None)
        prev_side = side
    return warning, confirmation


def pct(a, b):
    return round(100*a/b, 2) if b else None


def summarize(episodes, method):
    tp = fp = tn = fn = 0
    warned = true_warnings = confirmed = sustained_confirmations = true_captured = 0
    delays = []
    examples = []
    for x in episodes:
        flat = x[method]
        stable = not x['oracle']['breakout']
        if flat and stable: tp += 1
        elif flat and not stable: fp += 1
        elif not flat and not stable: tn += 1
        else: fn += 1
        if not flat:
            continue
        warning, confirmation = x['warning'], x['confirmation']
        if warning is not None:
            warned += 1
            true_warnings += not stable
        if confirmation is not None:
            confirmed += 1
            j, side, first = confirmation
            # Success against the SAME 12h future oracle, not a tautological
            # claim that two closes constitute a true breakout.
            if not stable and x['oracle']['side'] == side and j <= x['oracle']['third']:
                sustained_confirmations += 1
                true_captured += 1
                delays.append(j-first)
            # Followup may be insufficient; don't label false here if the
            # oracle says breakout in the other direction or outside window.
        if len(examples) < 5 and not stable:
            examples.append(x['time'].isoformat())
    total = len(episodes)
    stable_total = tp+fn
    breakout_total = fp+tn
    return {
        'episodes':total, 'flat_predictions':tp+fp, 'stable_proxy_episodes':stable_total,
        'breakout_proxy_episodes':breakout_total,
        'confusion_flat_TP_FP_TN_FN':{'TP':tp,'FP':fp,'TN':tn,'FN':fn},
        'overall_proxy_accuracy_pct':pct(tp+tn,total),
        'balanced_proxy_accuracy_pct':round((tp/stable_total+tn/breakout_total)*50,2) if stable_total and breakout_total else None,
        'flat_precision_pct':pct(tp,tp+fp), 'flat_recall_pct':pct(tp,stable_total),
        'breakout_risk_among_predicted_flat_pct':pct(fp,tp+fp),
        'breakout_rejection_recall_pct':pct(tn,breakout_total),
        'flat_episode_warnings':warned,
        'warning_breakout_precision_pct':pct(true_warnings,warned),
        'confirmed_breakouts':confirmed,
        'confirmed_matched_future_oracle_pct':pct(sustained_confirmations,confirmed),
        'confirmed_capture_of_breakouts_while_flagged_flat_pct':pct(true_captured,fp),
        'median_confirmation_lag_from_first_outside_close_hours':statistics.median(delays) if delays else None,
    }


def collect(h1, h4, h4_atr, begin, end):
    """One calendar-anchored opportunity per day, never overlap 12h targets."""
    closes = [b[0]+FOUR for b in h4]
    selected = []
    stats = Counter()
    for i, (t, *_rest) in enumerate(h1):
        if t < begin or t >= end or t.hour != 0:
            continue
        stats['calendar_midnight_opportunities'] += 1
        if i < PROFILE_HOURS or i+HORIZON > len(h1):
            stats['censored_missing_h1'] += 1
            continue
        past = h1[i-PROFILE_HOURS:i]
        future = h1[i:i+HORIZON]
        if not contiguous(past,t-PROFILE_HOURS*HOUR) or not contiguous(future,t) or future[-1][0]+HOUR > end:
            stats['censored_gap_or_segment_boundary'] += 1
            continue
        h4_index = bisect.bisect_right(closes,t)-1
        if h4_index < 200 or not h4_atr[h4_index] or h4_atr[h4_index] <= 0:
            stats['censored_h4_atr_warmup'] += 1
            continue
        h4_value = h4_atr[h4_index]
        p = profile(past)
        width = p['high']-p['low']
        ratio = range_atr_signal(past)
        if ratio is None or width <= 0:
            stats['censored_invalid_atr'] += 1
            continue
        tpo = 2*h4_value <= width <= 10*h4_value and p['va_width'] <= TPO_MAX_VA_H4_ATR*h4_value
        ra = ratio < RATIO_THRESHOLD
        outcome = future_oracle(future,p['low'],p['high'])
        warning, confirmation = alerts(future,p['low'],p['high'])
        selected.append({'time':t,'lower':p['low'],'upper':p['high'],
                         'ratio':round(ratio,6),'va_over_h4_atr':round(p['va_width']/h4_value,6),
                         'tpo':tpo,'range_atr':ra,'combined':tpo and ra,
                         'oracle':outcome,'warning':warning,'confirmation':confirmation})
    stats['evaluated_common_episodes'] = len(selected)
    return selected, dict(stats)


def main():
    if len(sys.argv)!=2:
        raise SystemExit('Usage: python src/compare_tpo_atr_h1.py data/eurusd_h1.csv')
    path = Path(sys.argv[1]); h1=load_h1(path); h4=load_h4(path)
    _,_,h4_atr,_ = indicators(h4)
    split=int(len(h4)*.7)
    split_time=h4[split][0]
    end=h4[-1][0]+FOUR
    out={'data_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
         'configuration':{'sampling':'one opportunity per UTC midnight, disjoint future 12h windows',
                          'common_bounds':'80 contiguous prior H1 extremes (frozen)',
                          'tpo':'80 prior H1 high-low bin occupancy, 1 pip, 70pct Value Area; width 2-10 completed H4 ATR14 and VA width <=4 H4 ATR14',
                          'range_atr':'20 prior H1 range / simple ATR14(H1) <5',
                          'combined':'both flat flags true',
                          'future_oracle':'NEXT 12 contiguous H1: 3 same-side consecutive closes outside frozen 80h extremes; final close >=5pct frozen width beyond edge',
                          'alerts':'future wick warning; two consecutive closes same side confirm; only evaluated if method said flat'},
         'split_utc':split_time.isoformat(), 'experiments':{},
         'limitations':'Proxy-based matched comparison, NOT true regime accuracy. 2023-26 reused inspected history, NOT independent OOS. One midnight sample/day may miss intraday episodes. TPO H1 high-low approximation, NOT genuine Sierra TPO or volume. H4 ATR Wilder, H1 ATR SMA as in supplied formula. No v3 regime filter: each detector separately tested. Confirmation vs oracle within same future window, no guaranteed post-confirmation persistence; median delays on matched detections only. NOT a trading PnL test. No orders.'}
    Path('reports').mkdir(exist_ok=True)
    for name, begin, finish in (('development',h4[200][0],split_time),('previously_inspected_2023_2026',split_time,end)):
        episodes,audit=collect(h1,h4,h4_atr,begin,finish)
        out['experiments'][name]={'audit':audit, 'baseline_always_flat':{
            'stable_proxy_prevalence_pct':pct(sum(not e['oracle']['breakout'] for e in episodes),len(episodes))},
            'methods':{method:summarize(episodes,method) for method in METHODS}}
        with (Path('reports')/f'tpo_atr_comparison_{name}.csv').open('w',newline='') as f:
            w=csv.writer(f)
            w.writerow(['utc','lower','upper','range_to_atr','va_to_h4_atr','tpo_flat','range_atr_flat','combined_flat','future_sustained_breakout','breakout_direction','first_outside_close_index','third_outside_close_index','warning_index','confirmation_index','confirmation_direction'])
            for x in episodes:
                w.writerow([x['time'].isoformat(),x['lower'],x['upper'],x['ratio'],x['va_over_h4_atr'],int(x['tpo']),int(x['range_atr']),int(x['combined']),int(x['oracle']['breakout']),x['oracle']['side'],x['oracle']['first'],x['oracle']['third'],x['warning'],x['confirmation'][0] if x['confirmation'] else None,x['confirmation'][1] if x['confirmation'] else None])
        print(name,json.dumps(out['experiments'][name],sort_keys=True))
    (Path('reports')/'tpo_atr_comparison.json').write_text(json.dumps(out,indent=2)+'\n')
    print('RESEARCH ONLY: behavioral labels, not true flat classification accuracy. No trading.')


if __name__=='__main__':
    main()
