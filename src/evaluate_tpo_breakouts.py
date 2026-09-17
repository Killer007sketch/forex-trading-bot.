"""Research-only EVENT-level audit of frozen TPO range exits; no orders.

A future-outcome PROXY is not an independently labelled market regime or a
predictive accuracy guarantee. Frozen boundaries and alerts come from the
causal tpo_range_h1 detector. Evaluate only after the signal, with complete
contiguous H1 follow-up, and deduplicate repeated alerts within a cycle.
"""
import csv
import datetime as dt
import hashlib
import json
import statistics
import sys
from pathlib import Path

from range_guard_h1 import load_h1
from regime_v3_h4 import WARMUP, classify_v3, wilson_lower
from tpo_range_h1 import detect
from trend_h4 import indicators, load_h4

HOUR = dt.timedelta(hours=1)
FOUR = dt.timedelta(hours=4)
HORIZON = 12
EXTENSION = .05  # fraction of frozen outer-channel width (not an optimized threshold)


def group_cycles(rows):
    """One first warning and one confirmation per frozen profile cycle."""
    cycles = []
    for row in rows:
        time, kind, low, high, poc, val, vah = row
        if kind == 'start':
            cycles.append({'start': time, 'low': low, 'high': high,
                           'warnings': [], 'confirmation': None})
        elif not cycles:
            raise ValueError('Alert without a preceding profile')
        elif kind == 'warning':
            cycles[-1]['warnings'].append(time)
        elif kind == 'confirmed_breakout':
            if cycles[-1]['confirmation'] is not None:
                raise ValueError('Multiple confirmations in a single cycle')
            cycles[-1]['confirmation'] = time
        else:
            raise ValueError('Unknown TPO event: ' + kind)
    return cycles


def future_exit(h1, index, low, high, end_time, offset=0):
    """Offline oracle: within 12 contiguous H1 bars, three successive closes
    outside the SAME edge; the third extends >=5% of frozen channel width.
    A warning uses bars index..index+11; confirmed signal evaluation uses
    strictly FUTURE bars index+1..index+12. Return None if censored/gappy.
    First exterior close and third-close timestamps are kept for delay audit.
    """
    start = index + offset
    finish = start + HORIZON
    if start < 0 or finish > len(h1):
        return None
    block = h1[start:finish]
    if block[-1][0] >= end_time or any(block[j][0]-block[j-1][0] != HOUR for j in range(1, len(block))):
        return None
    if index >= 0 and start > index and block[0][0]-h1[index][0] != HOUR:
        return None
    streak = side = 0
    first = None
    margin = EXTENSION*(high-low)
    for j, bar in enumerate(block):
        close = bar[4]
        direction = 1 if close > high else -1 if close < low else 0
        if direction == 0:
            streak, side, first = 0, 0, None
            continue
        if direction != side:
            streak, side, first = 1, direction, start+j
        else:
            streak += 1
        if streak >= 3 and (close-high >= margin if side == 1 else low-close >= margin):
            return {'sustained': True, 'side': side, 'first_close': first,
                    'third_close': start+j}
    return {'sustained': False, 'side': 0, 'first_close': None, 'third_close': None}


def interval(wins, total):
    return {'n': total, 'success': wins, 'rate_pct': round(100*wins/total, 2) if total else None,
            'wilson_95pct_lower_pct': round(100*wilson_lower(wins,total), 2) if total >= 30 else None}


def score(h1, rows, end_time):
    index_by_time = {bar[0].isoformat(): i for i, bar in enumerate(h1)}
    cycles = group_cycles(rows)
    warning_valid = warning_true = 0
    confirm_valid = confirm_persistent = 0
    true_warnings_detected = 0
    lag_from_first_close = []
    lag_from_warning = []
    censored_warnings = censored_confirmations = 0
    cases = []
    for number, cycle in enumerate(cycles, 1):
        first_warning = cycle['warnings'][0] if cycle['warnings'] else None
        confirmed = cycle['confirmation']
        warning_label = confirm_label = None
        matched = False
        if first_warning:
            warn_idx = index_by_time[first_warning]
            warning_label = future_exit(h1, warn_idx, cycle['low'], cycle['high'], end_time)
            if warning_label is None:
                censored_warnings += 1
            else:
                warning_valid += 1
                warning_true += int(warning_label['sustained'])
        if confirmed:
            conf_idx = index_by_time[confirmed]
            confirm_label = future_exit(h1, conf_idx, cycle['low'], cycle['high'], end_time, offset=1)
            if confirm_label is None:
                censored_confirmations += 1
            else:
                confirm_valid += 1
                confirm_persistent += int(confirm_label['sustained'])
        if warning_label is not None and warning_label['sustained'] and confirmed:
            conf_idx = index_by_time[confirmed]
            confirmed_side = (1 if h1[conf_idx][4] > cycle['high'] else
                              -1 if h1[conf_idx][4] < cycle['low'] else 0)
            matched = (confirmed_side == warning_label['side'] and
                       index_by_time[first_warning] <= conf_idx <= index_by_time[first_warning]+HORIZON-1)
            if matched:
                true_warnings_detected += 1
                lag_from_first_close.append(conf_idx-warning_label['first_close'])
                lag_from_warning.append(conf_idx-index_by_time[first_warning])
        cases.append({'cycle': number, 'started_at_h1_open_utc':cycle['start'],
                      'lower':cycle['low'],'upper':cycle['high'],
                      'first_warning_h1_open_utc':first_warning,
                      'duplicate_warning_events':max(0,len(cycle['warnings'])-1),
                      'confirmation_h1_open_utc':confirmed,
                      'sustained_within_12h_after_warning':None if warning_label is None else warning_label['sustained'],
                      'sustained_12h_after_confirmation':None if confirm_label is None else confirm_label['sustained'],
                      'true_warning_detected_by_confirmation':matched})
    return {'cycles':len(cycles),
            'cycles_without_warning':sum(not c['warnings'] for c in cycles),
            'cycles_with_warning':sum(bool(c['warnings']) for c in cycles),
            'raw_warning_events':sum(len(c['warnings']) for c in cycles),
            'duplicate_warning_events':sum(max(0,len(c['warnings'])-1) for c in cycles),
            'confirmed_cycles':sum(c['confirmation'] is not None for c in cycles),
            'warning_sustained_exit_proxy':interval(warning_true,warning_valid),
            'warning_false_alarm_proxy_pct':round(100*(warning_valid-warning_true)/warning_valid,2) if warning_valid else None,
            'confirmation_future_persistence_proxy':interval(confirm_persistent,confirm_valid),
            'confirmation_capture_among_true_warning_events':interval(true_warnings_detected,warning_true),
            'median_confirmation_lag_after_first_outside_close_hours':statistics.median(lag_from_first_close) if lag_from_first_close else None,
            'median_warning_to_confirmation_hours':statistics.median(lag_from_warning) if lag_from_warning else None,
            'censored_warnings_missing_12h_followup':censored_warnings,
            'censored_confirmations_missing_12h_followup':censored_confirmations}, cases


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python src/evaluate_tpo_breakouts.py data/eurusd_h1.csv')
    data=Path(sys.argv[1])
    h1=load_h1(data)
    h4=load_h4(data)
    labels,_,evidence=classify_v3(h4)
    _,_,atr,_=indicators(h4)
    split=int(len(h4)*.7)
    result={'data_sha256':hashlib.sha256(data.read_bytes()).hexdigest(),
            'definition':'Independent future price-behavior proxy: within 12 consecutive H1 closes after first warning (including warning bar), >=3 consecutive closes beyond one frozen outer boundary, third >=5% channel width farther outside. Confirmed alert persistence uses strictly NEXT 12 H1 bars. 1st warning deduplicated per cycle. Censor incomplete/gappy followup.',
            'experiments':{},'limitations':'NOT ground-truth trend/range accuracy, NOT market-wide breakout recall: true exits are evaluated only in detector-armed cycles following its warning. The warning detects any outer-channel wick by definition, so warning recall would be tautological. Future oracle is retrospectively evaluated, NEVER used to generate trading signals. Previous 2023-2026 holdout repeatedly inspected; no independent OOS. H1 candle approximation; no tick/order-flow, no trades.'}
    Path('reports').mkdir(exist_ok=True)
    for maximum in (4.0,6.0):
        name=f'va_width_le_{maximum:g}_atr'
        result['experiments'][name]={}
        for segment,first,last in (('development',WARMUP,split),('historical_holdout',split,len(h4))):
            _,rows=detect(h1,h4,labels,evidence,atr,first,last,maximum)
            end=h4[last-1][0]+dt.timedelta(hours=4)
            metrics,cases=score(h1,rows,end)
            result['experiments'][name][segment]=metrics
            print(name,segment,json.dumps(metrics,sort_keys=True))
            with (Path('reports')/f'tpo_accuracy_{name}_{segment}.csv').open('w',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=list(cases[0]) if cases else ['cycle'])
                writer.writeheader()
                writer.writerows(cases)
    (Path('reports')/'tpo_breakout_validation.json').write_text(json.dumps(result,indent=2)+'\n')
    print('DIAGNOSTIC ONLY: behavioral event proxies, not proven predictive accuracy. Live trading disabled.')


if __name__=='__main__':
    main()
