"""Research-only EURUSD TPO approximation from bid H1 OHLC; NO orders.
One H1 candle counts once at EACH 2-pip row in its high-low range. This does
NOT reconstruct true occupancy, volume, Sierra Chart internals, or bid/ask.
All labels exist only after the corresponding H4 candle closes.
Method reference: https://www.sierrachart.com/index.php?l=doc/StudiesReference/TimePriceOpportunityCharts.html
"""
import csv
import datetime as dt
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path

from range_guard_h1 import load_h1
from regime_v3_h4 import WARMUP, classify_v3
from trend_h4 import indicators, load_h4

HOUR = dt.timedelta(hours=1)
H4 = dt.timedelta(hours=4)
TICK = 0.00001
STEP_TICKS = 20  # 2 pips; exploratory, not Sierra Chart's default.
LOOKBACK = 20  # Completed H4 bars / 80 H1 time blocks.
HORIZON_H1 = 24


def profile(h1_window, step_ticks=STEP_TICKS, coverage=.70):
    """POC ties: closest to profile midpoint then lower. VA expands contiguously."""
    if not h1_window or step_ticks <= 0 or not 0 < coverage <= 1:
        raise ValueError('Invalid profile parameters')
    counts = Counter()
    for _, _, high, low, _ in h1_window:
        a = round(low / TICK) // step_ticks
        b = round(high / TICK) // step_ticks
        for row in range(a, b + 1):
            counts[row] += 1
    floor, ceiling = min(counts), max(counts)
    midpoint = (floor + ceiling) / 2
    maximum = max(counts.values())
    poc = min((k for k, v in counts.items() if v == maximum),
              key=lambda k: (abs(k - midpoint), k))
    lower = upper = poc
    total = sum(counts.values())
    included = counts[poc]
    while included < coverage * total:
        down = counts[lower - 1] if lower > floor else -1
        up = counts[upper + 1] if upper < ceiling else -1
        if down < 0 and up < 0:
            break
        if down >= up and down >= 0:
            lower -= 1
            included += counts[lower]
        if up >= down and up >= 0:
            upper += 1
            included += counts[upper]
    step = step_ticks * TICK
    return {'poc': poc * step, 'val': lower * step, 'vah': (upper + 1) * step,
            'low': floor * step, 'high': (ceiling + 1) * step,
            'actual_coverage': included / total, 'total_tpo': total}


def build(h1, h4, atr):
    """Only 80 verified hourly candles matching 20 completed H4 bars are eligible."""
    at = {bar[0]: j for j, bar in enumerate(h1)}
    snapshots = [None] * len(h4)
    candidates = [False] * len(h4)
    labels = ['warmup'] * len(h4)
    previous_poc = None
    for i in range(max(WARMUP, LOOKBACK), len(h4)):
        j = at.get(h4[i][0] + 3 * HOUR)
        if j is None or j < LOOKBACK * 4 - 1 or atr[i] is None or atr[i] <= 0:
            previous_poc = None
            continue
        window = h1[j - LOOKBACK * 4 + 1:j + 1]
        if any(window[4*k + hour][0] != h4[i - LOOKBACK + 1 + k][0] + hour * HOUR
               for k in range(LOOKBACK) for hour in range(4)):
            previous_poc = None
            continue
        p = profile(window)
        channel_low = min(b[3] for b in h4[i-LOOKBACK+1:i+1])
        channel_high = max(b[2] for b in h4[i-LOOKBACK+1:i+1])
        width = channel_high - channel_low
        va_width = p['vah'] - p['val']
        a = atr[i]
        p.update({'outer_low': channel_low, 'outer_high': channel_high,
                  'atr': a, 'available_utc': (h4[i][0] + H4).isoformat(),
                  'va_fraction_of_outer_width': va_width / width if width else None})
        snapshots[i] = p
        candidates[i] = (3*a <= width <= 10*a and va_width <= 2.5*a
                         and va_width <= .60*width and p['val'] <= h4[i][4] <= p['vah']
                         and previous_poc is not None and abs(p['poc'] - previous_poc) <= .5*a)
        labels[i] = 'range' if candidates[i] and candidates[i-1] else 'transition'
        previous_poc = p['poc']
    return snapshots, labels


def outcomes(h1, h4, snapshots, labels, start, end):
    at = {bar[0]: j for j, bar in enumerate(h1)}
    events = []
    next_allowed = -1
    for i in range(max(WARMUP + 1, start), end):
        if labels[i] != 'range' or labels[i-1] == 'range' or not snapshots[i]:
            continue
        first = at.get(h4[i][0] + H4)
        if first is None or first < next_allowed or first + HORIZON_H1 > len(h1):
            continue
        window = h1[first:first + HORIZON_H1]
        if end < len(h4) and window[-1][0] >= h4[end][0]:
            continue
        # Only contiguous H1 or a normal weekend break; no invented missing hours.
        if any(b[0] - a[0] != HOUR and not (a[0].weekday() == 4
                   and b[0].weekday() in (6, 0)
                   and b[0] - a[0] <= dt.timedelta(hours=72))
               for a, b in zip(window, window[1:])):
            continue
        p = snapshots[i]
        low, high = p['outer_low'], p['outer_high']
        wick = any(b[3] < low or b[2] > high for b in window)
        close = any(b[4] < low or b[4] > high for b in window)
        streak, side, confirmed_at = 0, 0, None
        for k, b in enumerate(window):
            direction = 1 if b[4] > high else -1 if b[4] < low else 0
            streak = streak + 1 if direction and direction == side else 1 if direction else 0
            side = direction
            if streak == 2:
                confirmed_at = k + 1
                break
        events.append({'label_at_h4_close_utc':p['available_utc'], 'outer_low':low,
                       'outer_high':high, 'poc':p['poc'], 'val':p['val'], 'vah':p['vah'],
                       'wick_breach_24h':int(wick), 'close_breach_24h':int(close),
                       'two_close_breakout_24h':int(confirmed_at is not None),
                       'two_close_confirmation_hour':confirmed_at or ''})
        next_allowed = first + HORIZON_H1
    count = len(events)
    result = {'nonoverlap_events':count,
              'wick_containment_pct':round(100*sum(not e['wick_breach_24h'] for e in events)/count,2) if count else None,
              'close_containment_pct':round(100*sum(not e['close_breach_24h'] for e in events)/count,2) if count else None,
              'confirmed_two_close_breakout_pct':round(100*sum(e['two_close_breakout_24h'] for e in events)/count,2) if count else None,
              'wick_only_alarm_pct':round(100*sum(e['wick_breach_24h'] and not e['close_breach_24h'] for e in events)/count,2) if count else None}
    return result, events


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python src/tpo_range_h4.py data/eurusd_h1.csv')
    data = Path(sys.argv[1])
    h1, h4 = load_h1(data), load_h4(data)
    _, _, atr, _ = indicators(h4)
    snapshots, tpo_labels = build(h1, h4, atr)
    baseline, _, _ = classify_v3(h4)
    split = int(len(h4)*.7)
    report = {'sha256':hashlib.sha256(data.read_bytes()).hexdigest(), 'h4_bars':len(h4),
              'method':'H1 high-low approximate TPO, 2-pip rows, 20 complete H4; POC and 70% VA; two completed-H4 confirmations',
              'segments':{},
              'limitations':'Bid-only H1 OHLC gives visited-price proxy, not true time at price or Sierra exact TPO. 24 trading H1 bars span weekends. Previously inspected 2023-26 is NOT untouched holdout. No ground-truth labels, execution, profitability or proven accuracy. Thresholds exploratory; signals only after H4 close.'}
    Path('reports').mkdir(exist_ok=True)
    for segment, start, end in (('development',WARMUP,split),('previously_inspected',split,len(h4))):
        report['segments'][segment] = {}
        for name, labels in (('tpo',tpo_labels), ('v3_reference',baseline)):
            n = end-start
            result, events = outcomes(h1,h4,snapshots,labels,start,end)
            result['range_h4_bars'] = sum(x == 'range' for x in labels[start:end])
            result['share_range_pct'] = round(100 * result['range_h4_bars']/n,2)
            report['segments'][segment][name] = result
            with (Path('reports')/f'tpo_{segment}_{name}_events.csv').open('w',newline='') as f:
                writer = csv.DictWriter(f,fieldnames=['label_at_h4_close_utc','outer_low','outer_high','poc','val','vah','wick_breach_24h','close_breach_24h','two_close_breakout_24h','two_close_confirmation_hour'])
                writer.writeheader(); writer.writerows(events)
    with (Path('reports')/'tpo_profiles_tail.csv').open('w',newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['available_utc','label','poc','val','vah','outer_low','outer_high','tpo_coverage'])
        for i in range(max(0,len(h4)-100),len(h4)):
            p=snapshots[i]
            if p:
                writer.writerow([p['available_utc'],tpo_labels[i],p['poc'],p['val'],p['vah'],p['outer_low'],p['outer_high'],p['actual_coverage']])
    (Path('reports')/'tpo_range_h4.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
    print('RESEARCH ONLY: No orders, no demonstrated classification accuracy or predictive reliability.')


if __name__ == '__main__':
    main()
