"""Research only: causal EURUSD H1 TPO approximation, frozen range, H1 exit alerts.

Each H1 OHLC high-low contributes ONE TPO to each touched 1-pip price bin.
This is NOT Sierra Chart's intrabar TPO, genuine traded volume or tick data.
The frozen 80-H1 profile uses observations STRICTLY before the alert's H1 open.
No broker orders, optimized thresholds, or assertions of classification accuracy.
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
from trend_h4 import PIP, indicators, load_h4

HOUR = dt.timedelta(hours=1)
FOUR = dt.timedelta(hours=4)
PROFILE_HOURS = 80
VALUE_FRACTION = .70


def profile(bars, tick=PIP, fraction=VALUE_FRACTION):
    """Return immutable range low/high, POC/VAL/VAH and diagnostic VA width.

    POC tie: price level closest to range midpoint; secondary tie lower level.
    Value area: expand one adjacent price level at a time toward greater TPO;
    a tie expands lower first. This is our documented approximation, not a
    claim of bit-for-bit Sierra Chart equivalence.
    """
    if not bars or tick <= 0 or not 0 < fraction <= 1:
        raise ValueError('Invalid profile settings')
    lo = min(b[3] for b in bars)
    hi = max(b[2] for b in bars)
    low_bin = math.floor((lo + 1e-10) / tick)
    high_bin = math.floor((hi + 1e-10) / tick)
    if high_bin-low_bin > 3000:
        raise ValueError('Profile too wide')
    counts = Counter()
    for bar in bars:
        first = math.floor((bar[3]+1e-10)/tick)
        last = math.floor((bar[2]+1e-10)/tick)
        for p in range(first, last+1):
            counts[p] += 1
    mid = (low_bin+high_bin)/2
    poc = min(range(low_bin, high_bin+1), key=lambda p: (-counts[p], abs(p-mid), p))
    left = right = poc
    mass = counts[poc]
    target = math.ceil(sum(counts.values())*fraction)
    while mass < target:
        lower = counts[left-1] if left > low_bin else -1
        upper = counts[right+1] if right < high_bin else -1
        if lower >= upper and lower >= 0:
            left -= 1
            mass += counts[left]
        elif upper >= 0:
            right += 1
            mass += counts[right]
        else:
            break
    return {'low':lo, 'high':hi, 'poc':round(poc*tick, 5),
            'val':round(left*tick, 5), 'vah':round((right+1)*tick, 5),
            'va_width':(right-left+1)*tick, 'tpo_total':sum(counts.values()),
            'bins':high_bin-low_bin+1}


def contiguous(window, activation):
    return (len(window) == PROFILE_HOURS and
            window[-1][0]+HOUR == activation and
            all(window[j][0]-window[j-1][0] == HOUR for j in range(1, len(window))))


def detect(h1, h4, labels, evidence, atr, start, end, max_va_atr):
    """Per-segment independent state machine, H4 signal only after H4 close.

    Once frozen, first H1 high/low beyond channel is a WARNING. Two adjacent
    H1 closes strictly beyond SAME edge confirm an exit; a return is logged
    as unconfirmed. No same-hour knowledge for decisions at its OPEN.
    A stopped cycle cannot re-arm without a non-range H4 state followed by
    two completed range H4 states. Stop/rearm counts are diagnostics only.
    """
    begin = h4[start][0]
    finish = h4[end-1][0]+FOUR
    ptr = 0
    current = -1
    seen = -1
    armed = True
    frozen = None
    outside_side = 0
    outside_count = 0
    prior_time = None
    events = Counter()
    rows = []
    for k, (time, op, high, low, close) in enumerate(h1):
        if time < begin:
            continue
        if time >= finish:
            break
        gap = prior_time is not None and time-prior_time != HOUR
        prior_time = time
        if gap and frozen is not None:
            events['data_gap_shutdown'] += 1
            frozen, armed = None, False
            outside_side = outside_count = 0
        while ptr < len(h4) and h4[ptr][0]+FOUR <= time:
            current = ptr
            ptr += 1
        if current != seen:
            seen = current
            if current >= 0 and labels[current] != 'range':
                if frozen is not None:
                    events['regime_shutdown'] += 1
                    frozen = None
                    outside_side = outside_count = 0
                armed = True
            if (frozen is None and armed and not gap and current >= max(start, WARMUP+1)
                    and labels[current] == labels[current-1] == 'range'
                    and evidence[current] >= 5 and atr[current] and atr[current] > 0):
                window = h1[k-PROFILE_HOURS:k] if k >= PROFILE_HOURS else []
                if not contiguous(window, time):
                    events['incomplete_profile_rejected'] += 1
                else:
                    p = profile(window)
                    width = p['high']-p['low']
                    a = atr[current]
                    if 2*a <= width <= 10*a and p['va_width'] <= max_va_atr*a:
                        frozen = (p, time, current)
                        events['cycles_started'] += 1
                        rows.append((time.isoformat(), 'start', p['low'], p['high'],
                                     p['poc'], p['val'], p['vah']))
                    else:
                        events['shape_rejected'] += 1
        if frozen is None:
            continue
        p, activation, source = frozen
        if current < 0 or time > h4[current][0]+2*FOUR:
            events['stale_shutdown'] += 1
            frozen, armed = None, False
            outside_side = outside_count = 0
            continue
        # Both external wicks in one hour: direction is ambiguous; no confirmed
        # direction may be inferred from unobserved intrabar path.
        outside_high = high > p['high']
        outside_low = low < p['low']
        if outside_high or outside_low:
            if not outside_count:
                events['initial_boundary_warning'] += 1
                rows.append((time.isoformat(), 'warning', p['low'], p['high'],
                             p['poc'], p['val'], p['vah']))
        direction = 1 if close > p['high'] else -1 if close < p['low'] else 0
        if direction:
            if direction == outside_side:
                outside_count += 1
            else:
                outside_side, outside_count = direction, 1
            if outside_count >= 2:
                events['confirmed_close_breakout'] += 1
                rows.append((time.isoformat(), 'confirmed_breakout', p['low'], p['high'],
                             p['poc'], p['val'], p['vah']))
                frozen, armed = None, False
                outside_side = outside_count = 0
        elif outside_side:
            events['unconfirmed_close_breakout'] += 1
            outside_side = outside_count = 0
        if frozen is not None and (outside_low or outside_high) and direction == 0:
            events['wick_only_boundary_touch'] += 1
    if frozen is not None:
        events['open_cycle_at_segment_end'] += 1
    return {'events':dict(events), 'sample_event_rows':len(rows)}, rows


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python src/tpo_range_h1.py data/eurusd_h1.csv')
    path = Path(sys.argv[1])
    h1 = load_h1(path)
    h4 = load_h4(path)
    labels, _, evidence = classify_v3(h4)
    _, _, atr, _ = indicators(h4)
    split = int(len(h4)*.7)
    result = {'data_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
              'h1_bars':len(h1), 'h4_bars':len(h4), 'split_utc':h4[split][0].isoformat(),
              'method':'80 contiguous H1 OHLC-range occupancy, 1-pip bins, 70% TPO area; frozen outer highs/lows; H1 wick warning and 2-close confirmation',
              'experiments':{},
              'limitations':'H1 TPO is range-touch approximation, NOT actual intrahour time, volume, Sierra-equivalent engine, or broker prices. No independent regime truth; confirmed breakout is an operational definition, not correct-exit rate. Already-inspected historical holdout is NOT untouched. No trades or live orders.'}
    Path('reports').mkdir(exist_ok=True)
    for maximum in (4.0, 6.0):
        name = f'va_width_le_{maximum:g}_atr'
        result['experiments'][name] = {}
        for segment, first, last in (('development',WARMUP,split),('historical_holdout',split,len(h4))):
            metrics, rows = detect(h1,h4,labels,evidence,atr,first,last,maximum)
            result['experiments'][name][segment] = metrics
            with (Path('reports')/f'tpo_{name}_{segment}.csv').open('w',newline='') as file:
                writer=csv.writer(file)
                writer.writerow(['h1_utc','event','frozen_lower','frozen_upper','poc','val','vah'])
                writer.writerows(rows)
            print(name,segment,json.dumps(metrics,sort_keys=True))
    (Path('reports')/'tpo_range_h1.json').write_text(json.dumps(result,indent=2)+'\n')
    print('RESEARCH ONLY: operational event counts do not measure predictive accuracy; live trading disabled.')


if __name__ == '__main__':
    main()
