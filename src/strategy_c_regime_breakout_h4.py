"""Offline research: fixed Strategy C hypothesis. No broker orders.

H4 EMA50/200 alignment, ADX14 >=25, 20-completed-H4 breakout, completed H1
Range/ATR >=5 (do not fade ranges). Only completed data at signal. One open
position; 0.5% risk, 4x notional cap, EUR 1000 sizing. Stop 2 H4 ATR, fixed
NET take profit 3R. Bid OHLC; SL precedes TP for ambiguous candles, gap stops
filled at adverse open, static hypothetical spread/slippage. No swap, real
broker fill, commission or margin. 2023-26 was previously inspected, not OOS.
"""
import csv
import datetime as dt
import hashlib
import json
import math
import sys
from pathlib import Path
from aggressive_range_backtest_h1 import COSTS, INITIAL_USD, LOT_UNITS
from compare_tpo_atr_h1 import range_atr_signal, contiguous
from range_guard_h1 import execution, load_h1
from trend_h4 import PIP, indicators, load_h4

HOUR = dt.timedelta(hours=1)
FOUR = dt.timedelta(hours=4)
RISK = .005
CAP = 4.
LOOKBACK = 20
ADX_THRESHOLD = 25.
STOP_ATR = 2.
RR = 3.


def levels(side, entry, atr, spread, slip):
    distance = STOP_ATR * atr
    def bid_for_exit(exit_fill):
        return exit_fill + slip * PIP if side == 1 else exit_fill - (spread + slip) * PIP
    stop = bid_for_exit(entry - side * distance)
    target = bid_for_exit(entry + side * RR * distance)
    assert abs(side * (execution(stop, -side, spread, slip) - entry) + distance) < 1e-9
    assert abs(side * (execution(target, -side, spread, slip) - entry) - RR * distance) < 1e-9
    return stop, target, distance


def signal(bars, i, fast, slow, atr, adx, flat_ratio):
    p = i - 1
    if i < 210 or atr[p] is None or adx[p] is None or flat_ratio is None:
        return 0
    if adx[p] < ADX_THRESHOLD or flat_ratio < 5.:
        return 0
    high = max(b[2] for b in bars[p-LOOKBACK:p])
    low = min(b[3] for b in bars[p-LOOKBACK:p])
    close = bars[p][4]
    return 1 if close > high and fast[p] > slow[p] else -1 if close < low and fast[p] < slow[p] else 0


def compute_gate(h1, h4):
    lookup = {b[0]: i for i, b in enumerate(h1)}
    ratios = []
    for t, *_ in h4:
        i = lookup.get(t)
        past = h1[i-21:i] if i is not None and i >= 21 else []
        ratios.append(range_atr_signal(past) if len(past) == 21 and contiguous(past, t-21*HOUR) else None)
    return ratios


def backtest(bars, begin, end, costs, gates, features):
    spread, slip = costs
    fast, slow, atr, adx = features
    balance = peak = INITIAL_USD
    maxdd = 0.
    position = None
    trades = []
    counts = {k: 0 for k in ('signals', 'entries', 'rejected_size', 'stops', 'targets',
                              'ambiguous_bars', 'stop_gaps', 'open_at_end', 'skipped_flat')}
    maxheld = 0.

    def equity(bid):
        if position is None:
            return balance
        return balance + position['side'] * position['units'] * (
            execution(bid, -position['side'], spread, slip) - position['entry'])

    def mark(bid):
        nonlocal peak, maxdd
        value = equity(bid)
        peak = max(peak, value)
        maxdd = max(maxdd, (peak-value)/peak)

    def close(bid, reason, when):
        nonlocal position, balance, peak, maxdd, maxheld
        p = position
        fill = execution(bid, -p['side'], spread, slip)
        pnl = p['side'] * p['units'] * (fill-p['entry'])
        balance += pnl
        peak = max(peak, balance)
        maxdd = max(maxdd, (peak-balance)/peak)
        held = (when-p['opened']).total_seconds()/3600
        maxheld = max(maxheld, held)
        trades.append({'open_utc': p['opened'].isoformat(), 'close_utc': when.isoformat(),
                       'side': 'long' if p['side']==1 else 'short',
                       'entry': round(p['entry'], 7), 'exit': round(fill, 7),
                       'stop_bid': round(p['stop'], 7), 'target_bid': round(p['target'], 7),
                       'units_eur': p['units'], 'risk_usd': round(p['risk_usd'], 5),
                       'pnl_usd': round(pnl, 5), 'pnl_R': round(pnl/p['risk_usd'], 6),
                       'holding_h': held, 'reason': reason})
        counts[reason] += 1
        position = None

    for i in range(begin, end):
        t, op, hi, lo, cl = bars[i]
        # A preexisting trade may close intrabar. No impossible same-bar reentry.
        if position is not None:
            p = position
            stopped = lo <= p['stop'] if p['side']==1 else hi >= p['stop']
            targeted = hi >= p['target'] if p['side']==1 else lo <= p['target']
            if stopped and targeted:
                counts['ambiguous_bars'] += 1
            if stopped:
                raw = min(op, p['stop']) if p['side']==1 else max(op, p['stop'])
                if (op < p['stop'] if p['side']==1 else op > p['stop']):
                    counts['stop_gaps'] += 1
                close(raw, 'stops', t+FOUR)
            elif targeted:
                close(p['target'], 'targets', t+FOUR)
            else:
                mark(cl)
            continue
        side = signal(bars, i, fast, slow, atr, adx, gates[i])
        if side:
            counts['signals'] += 1
            entry = execution(op, side, spread, slip)
            stop, target, risk = levels(side, entry, atr[i-1], spread, slip)
            if (side==1 and not stop < op < target) or (side==-1 and not target < op < stop):
                counts['rejected_size'] += 1
                continue
            units = int(math.floor(min(balance*RISK/risk, balance*CAP/entry)/LOT_UNITS)*LOT_UNITS)
            if units < LOT_UNITS:
                counts['rejected_size'] += 1
                continue
            position = {'opened': t, 'side': side, 'entry': entry, 'stop': stop,
                        'target': target, 'units': units, 'risk_usd': risk*units}
            counts['entries'] += 1
            stopped = lo <= stop if side==1 else hi >= stop
            targeted = hi >= target if side==1 else lo <= target
            if stopped and targeted:
                counts['ambiguous_bars'] += 1
            if stopped:
                close(min(op, stop) if side==1 else max(op, stop), 'stops', t+FOUR)
            elif targeted:
                close(target, 'targets', t+FOUR)
            else:
                mark(cl)
        elif gates[i] is not None and gates[i] < 5.:
            counts['skipped_flat'] += 1
    if position is not None:
        counts['open_at_end'] += 1
    final_equity = equity(bars[end-1][4])
    gross_win = sum(max(0, x['pnl_usd']) for x in trades)
    gross_loss = sum(max(0, -x['pnl_usd']) for x in trades)
    wins = sum(x['pnl_usd'] > 0 for x in trades)
    metrics = {'initial_usd': INITIAL_USD, 'realized_usd': round(balance, 2),
               'marked_equity_usd': round(final_equity, 2),
               'return_pct': round(100*(final_equity/INITIAL_USD-1), 2),
               'max_marked_dd_pct': round(maxdd*100, 2),
               'trades_closed': len(trades), 'wins': wins, 'losses': len(trades)-wins,
               'win_pct': round(100*wins/len(trades), 2) if trades else None,
               'mean_realized_R': round(sum(x['pnl_R'] for x in trades)/len(trades), 4) if trades else None,
               'profit_factor': round(gross_win/gross_loss, 3) if gross_loss else None,
               'max_holding_hours': maxheld, 'events': counts}
    return metrics, trades


def self_test():
    for side in (-1, 1):
        entry = execution(1.1, side, 1.5, .3)
        stop, target, dist = levels(side, entry, .002, 1.5, .3)
        assert abs(side*(execution(stop, -side, 1.5, .3)-entry)/dist+1) < 1e-10
        assert abs(side*(execution(target, -side, 1.5, .3)-entry)/dist-3) < 1e-10
    bars = [(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)+i*FOUR,
             1.1, 1.101, 1.099, 1.1) for i in range(230)]
    fast = [1.2]*230; slow = [1.1]*230; atr = [.002]*230; adx = [30.]*230
    bars[209] = (bars[209][0], 1.1, 1.105, 1.099, 1.104)
    assert signal(bars, 210, fast, slow, atr, adx, 6.) == 1
    assert signal(bars, 210, fast, slow, atr, adx, 4.) == 0
    print('C candidate: exact 3R economics, closed-bar signal and flat gate PASS')


def main():
    if len(sys.argv)==2 and sys.argv[1]=='--self-test':
        self_test();return
    if len(sys.argv)!=2:
        raise SystemExit('Usage: python src/strategy_c_regime_breakout_h4.py data/eurusd_h1.csv')
    path = Path(sys.argv[1]); h1 = load_h1(path); h4 = load_h4(path)
    gates = compute_gate(h1, h4)
    features = indicators(h4)
    periods = (('development_2016_2020', dt.datetime(2016,1,1,tzinfo=dt.timezone.utc), dt.datetime(2021,1,1,tzinfo=dt.timezone.utc)),
               ('validation_2021_2022', dt.datetime(2021,1,1,tzinfo=dt.timezone.utc), dt.datetime(2023,1,1,tzinfo=dt.timezone.utc)),
               ('previously_inspected_2023_2026', dt.datetime(2023,1,1,tzinfo=dt.timezone.utc), dt.datetime(2026,9,17,tzinfo=dt.timezone.utc)))
    report = {'strategy':'C: EMA50/200 + ADX>=25 + 20H4 breakout + H1 Range/ATR>=5',
              'risk_fraction':RISK,'stop_atr':STOP_ATR,'tp_R':RR,'notional_cap':CAP,
              'dataset_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
              'acceptance':'ALL periods and BOTH costs: net positive, PF>1, >=60 closed trades per period; else reject',
              'limits':'2023-26 previously inspected, no fresh OOS. BID H4, static hypothetical costs, excludes swaps, commissions, margin; no broker orders.',
              'periods':{}}
    Path('reports').mkdir(exist_ok=True)
    for name, start, stop in periods:
        begin = next(i for i,b in enumerate(h4) if b[0]>=start)
        end = next((i for i,b in enumerate(h4) if b[0]>=stop),len(h4))
        report['periods'][name] = {}
        for label, costs in COSTS.items():
            metrics, trades = backtest(h4, begin, end, costs, gates, features)
            report['periods'][name][label] = metrics
            outfile = Path('reports')/f'strategy_c_{name}_{label}_trades.csv'
            if trades:
                with outfile.open('w',newline='') as f:
                    writer=csv.DictWriter(f,fieldnames=list(trades[0]));writer.writeheader();writer.writerows(trades)
            print(name,label,json.dumps(metrics,sort_keys=True),flush=True)
    report['accepted'] = all(m['return_pct']>0 and m['profit_factor'] is not None and
                             m['profit_factor']>1 and m['trades_closed']>=60
                             for period in report['periods'].values() for m in period.values())
    (Path('reports')/'strategy_c_regime_breakout_report.json').write_text(json.dumps(report,indent=2)+'\n')
    print('PREDECLARED HISTORICAL ACCEPTANCE:',report['accepted'],'NO LIVE ORDERS',flush=True)

if __name__=='__main__':main()
