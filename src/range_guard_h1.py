"""Research only: v3 H4 range -> frozen channel, H1 circuit breaker and rearm.
Signals become available AFTER H4 closes; entries at later H1 opens. Dukascopy
BID bars are NOT broker executable prices. No live orders or broker connection.
"""
import csv
import datetime as dt
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path

from regime_v3_h4 import WARMUP, classify_v3, prequential_confidence
from trend_h4 import PIP, indicators, load_h4, parse_time

LOT_UNITS = 1000.0  # Hypothesis: 0.01 * 100,000 EUR; ProCent specification UNVERIFIED.
HOUR = dt.timedelta(hours=1)
FOUR_HOURS = dt.timedelta(hours=4)


def load_h1(path):
    result = []
    with path.open(newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            t = parse_time(row['timestamp'])
            o, h, l, c = (float(row[k]) for k in ('open', 'high', 'low', 'close'))
            if (result and t <= result[-1][0]) or t.minute or t.second or t.microsecond:
                raise ValueError('H1 timestamps must be hourly and increasing')
            if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)) or h < max(o, c, l) or l > min(o, c, h):
                raise ValueError('Invalid H1 OHLC')
            result.append((t, o, h, l, c))
    return result


def execution(raw_bid, side, spread, slip):
    """Side +1 buys at ask; -1 sells at bid, with adverse slippage."""
    return raw_bid + spread * PIP + slip * PIP if side == 1 else raw_bid - slip * PIP


def barrier(open_bid, high_bid, low_bid, low_limit, high_limit, side):
    """Conservative intrabar ordering: if both breached, adverse edge first."""
    bottom, top = low_bid <= low_limit, high_bid >= high_limit
    if side == 1:
        if bottom:
            return min(open_bid, low_limit), 'lower'
        if top:
            return max(open_bid, high_limit), 'upper'
    elif side == -1:
        if top:
            return max(open_bid, high_limit), 'upper'
        if bottom:
            return min(open_bid, low_limit), 'lower'
    elif bottom or top:
        return open_bid, 'no_position'
    return None


def units_for(balance, open_bid, side, low_limit, high_limit, spread, slip):
    entry = execution(open_bid, side, spread, slip)
    stop_bid = low_limit if side == 1 else high_limit - spread * PIP
    stop_exit = execution(stop_bid, -side, spread, slip)
    distance = side * (entry - stop_exit)
    if distance <= 0:
        return 0.0
    raw = min(balance * .005 / distance, balance * 5.0 / entry)
    return max(0.0, math.floor(raw / LOT_UNITS) * LOT_UNITS)


def simulate(h1, h4, labels, evidence, confidence, atr, start, end, spread, slip, early, confidence_gate):
    beginning, finishing = h4[start][0], h4[end-1][0] + FOUR_HOURS
    balance = peak = 1000.0
    drawdown = 0.0
    pointer, current, seen = 0, -1, -1
    channel = None
    position = None
    armed = True
    previous_time = None
    counts = Counter()
    trades = []

    def close(raw_bid, reason, timestamp):
        nonlocal position, balance, peak, drawdown
        if position is None:
            return
        side, entry, size, opened = position
        exit_price = execution(raw_bid, -side, spread, slip)
        pnl = side * size * (exit_price - entry)
        balance += pnl
        peak = max(peak, balance)
        drawdown = max(drawdown, (peak - balance) / peak)
        trades.append((opened.isoformat(), timestamp.isoformat(), side, size, round(pnl, 4), reason))
        counts['exit_' + reason] += 1
        position = None

    for t, op, hi, lo, cl in h1:
        if t < beginning:
            continue
        if t >= finishing:
            break
        gap = previous_time is not None and t - previous_time > HOUR
        previous_time = t
        if gap and channel is not None:
            close(op, 'data_gap', t)
            channel, armed = None, False
            counts['gap_shutdown'] += 1
        while pointer < len(h4) and h4[pointer][0] + FOUR_HOURS <= t:
            current = pointer
            pointer += 1
        if current != seen:
            seen = current
            if current >= 0 and labels[current] != 'range':
                if channel is not None:
                    close(op, 'regime_exit', t)
                    channel = None
                    counts['regime_shutdown'] += 1
                armed = True  # Only a non-range state can rearm a stopped cycle.
            if (not gap and channel is None and armed and current >= max(start, WARMUP + 1)
                    and labels[current] == labels[current - 1] == 'range' and evidence[current] >= 5
                    and (not confidence_gate or (confidence[current] is not None and confidence[current] >= 50))):
                low = min(b[3] for b in h4[current-19:current+1])
                high = max(b[2] for b in h4[current-19:current+1])
                a = atr[current]
                width = high - low
                if a and 3*a <= width <= 10*a and width >= 10*(spread + 2*slip)*PIP:
                    channel = (low, high, a)
                    counts['cycles_started'] += 1
                else:
                    counts['range_rejected_width'] += 1
        if channel is None:
            continue
        # If a H4 candle is missing, never trade on a stale regime signal.
        if current < 0 or t > h4[current][0] + 2*FOUR_HOURS:
            close(op, 'stale_signal', t)
            channel, armed = None, False
            counts['stale_shutdown'] += 1
            continue
        lower, upper, a = channel
        buffer = (.1 if early else 1.0) * a
        low_limit, high_limit = lower - buffer, upper + buffer
        # Market entries are made at the H1 OPEN, never retroactively at bar extremes.
        if position is None and lower <= op <= upper:
            width = upper - lower
            side = 1 if op <= lower + .22*width else -1 if op >= upper - .22*width else 0
            if side:
                size = units_for(balance, op, side, low_limit, high_limit, spread, slip)
                if size:
                    position = (side, execution(op, side, spread, slip), size, t)
                    counts['entries'] += 1
                else:
                    counts['rejected_lot_or_risk'] += 1
        side = position[0] if position else 0
        # Short adverse break is triggered on estimated ASK, not only bid high.
        upper_bid_trigger = high_limit - spread*PIP if side == -1 else high_limit
        breach = barrier(op, hi, lo, low_limit, upper_bid_trigger, side)
        if breach is not None:
            if position is not None:
                close(breach[0], 'h1_guard' if early else 'wide_h1_stop', t)
            channel, armed = None, False
            counts['boundary_shutdown'] += 1
            continue
        if position is not None:
            side, entry, size, opened = position
            midpoint = (lower + upper)/2
            if (side == 1 and hi >= midpoint) or (side == -1 and lo <= midpoint):
                raw_exit = max(op, midpoint) if side == 1 else min(op, midpoint)
                close(raw_exit, 'midpoint_take_profit', t)
            else:
                worst_bid = lo if side == 1 else hi
                marked = balance + side*size*(execution(worst_bid, -side, spread, slip) - entry)
                peak = max(peak, balance)
                drawdown = max(drawdown, (peak - marked)/peak)
    if position is not None:
        # Last available H1 candle within this segment, not next-segment information.
        last = next(bar for bar in reversed(h1) if beginning <= bar[0] < finishing)
        close(last[4], 'segment_end', last[0] + HOUR)
    years = (finishing-beginning).total_seconds()/(365.25*86400)
    return {'initial_usd':1000.0, 'final_usd':round(balance,2),
            'return_pct':round((balance/1000-1)*100,3),
            'annualized_pct':round(((balance/1000)**(1/years)-1)*100,3) if balance > 0 else -100.0,
            'max_hourly_worst_drawdown_pct':round(100*drawdown,3),
            'trades':len(trades), 'win_rate_pct':round(100*sum(x[4]>0 for x in trades)/len(trades),2) if trades else None,
            'net_pnl_usd':round(balance-1000,2), 'events':dict(counts)}, trades


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python src/range_guard_h1.py data/eurusd_h1.csv')
    data = Path(sys.argv[1])
    h1 = load_h1(data)
    h4 = load_h4(data)
    labels, _, evidence = classify_v3(h4)
    confidence, _ = prequential_confidence(h4, labels, [0]*len(h4), evidence)
    # Range proxy has no directional input; directional entries here are distinct.
    _, _, atr, _ = indicators(h4)
    split = int(len(h4)*.7)
    report = {'data_sha256':hashlib.sha256(data.read_bytes()).hexdigest(), 'h1':len(h1),'h4':len(h4),
              'holdout_start_utc':h4[split][0].isoformat(), 'experiments':{},
              'limitations':'RESEARCH ONLY. Historical 2023-2026 already inspected; NOT untouched OOS. Dukascopy BID-only, fixed spread/slippage; no variable spreads, news events, swaps, realistic margin, commissions, verified ProCent contract, broker execution. Intrabar both-edge adverse-first assumed; missing H1 triggers flat exit. Gate is historical range-outcome proxy, NOT calibrated certainty. No live orders.'}
    Path('reports').mkdir(exist_ok=True)
    for early in (True, False):
        for gated in (True, False):
            for cost, spread, slip in (('base',1.5,.3),('double',3.0,.6)):
                name = f"{'h1_guard' if early else 'wide_stop'}_{'historical_gate' if gated else 'votes_only'}_{cost}"
                report['experiments'][name] = {}
                for segment, s, e in (('development', WARMUP, split), ('historical_holdout', split, len(h4))):
                    result, trades = simulate(h1,h4,labels,evidence,confidence,atr,s,e,spread,slip,early,gated)
                    report['experiments'][name][segment] = result
                    print(name, segment, json.dumps(result,sort_keys=True))
                    with (Path('reports')/f'range_{name}_{segment}.csv').open('w',newline='') as f:
                        w=csv.writer(f)
                        w.writerow(['entry_h1_utc','exit_utc','side','units_eur','net_pnl_usd','exit_reason'])
                        w.writerows(trades)
    (Path('reports')/'range_guard_h1.json').write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
    print('RESEARCH ONLY. No profitable-edge or true-confidence claim; no broker connection.')


if __name__ == '__main__':
    main()
