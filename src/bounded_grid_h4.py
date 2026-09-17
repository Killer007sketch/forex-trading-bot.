"""Research only: bounded countertrend inventory; no broker API or live orders.

Signals are based on the previous completed H4 bar; orders fill at next H4 open.
Intrabar stop takes precedence over take profit when both are touched.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
from trend_h4 import load_h4, indicators, PIP


def simulate(bars, start, end, initial, spread, slip, risk, min_lot, levels, step_atr):
    _, _, atr, adx = indicators(bars)
    cash = peak = initial
    dd = 0.0
    legs = []
    trades = 0
    rejected = 0
    friction = (spread / 2 + slip) * PIP
    lot_units = min_lot * 100000  # Hypothesis only: verify ProCent contract and lot step.
    for i in range(max(start, 201), end):
        t, op, high, low, close = bars[i]
        p = i - 1
        volatility = atr[p]
        if volatility is None or volatility <= 0:
            continue
        # Realize basket at the open if yesterday's regime is no longer ranging.
        if legs and (adx[p] is None or adx[p] >= 25):
            side = legs[0][0]
            cash += sum(side * units * (op - side * friction - entry) for _, entry, units in legs)
            trades += len(legs)
            legs = []
        if legs:
            side = legs[0][0]
            units_total = sum(x[2] for x in legs)
            average = sum(x[1] * x[2] for x in legs) / units_total
            # Basket hard stop: total loss at stop approximates <= risk*levels at entry,
            # but gaps can exceed this. No margin liquidation modeled.
            stop = average - side * (2.5 * volatility)
            target = average + side * (0.65 * volatility)
            stop_hit = low <= stop if side == 1 else high >= stop
            target_hit = high >= target if side == 1 else low <= target
            if stop_hit or target_hit:
                raw = (min(op, stop) if side == 1 else max(op, stop)) if stop_hit else (max(op, target) if side == 1 else min(op, target))
                cash += sum(side * units * (raw - side * friction - entry) for _, entry, units in legs)
                trades += len(legs)
                legs = []
        # No same-bar reentry after basket liquidation (would use future OHLC).
                peak = max(peak, cash)
                dd = max(dd, (peak - cash) / peak)
                continue
        if cash <= 0:
            break
        if not legs and adx[p] is not None and adx[p] < 20:
            closes = [r[4] for r in bars[p-19:p+1]]
            mean = sum(closes) / len(closes)
            deviation = math.sqrt(sum((x-mean)**2 for x in closes)/len(closes))
            side = 1 if bars[p][4] < mean - 2*deviation else -1 if bars[p][4] > mean + 2*deviation else 0
            if side:
                entry = op + side * friction
                # Equal units per leg; no martingale or unconstrained leverage.
                units = math.floor(min(cash*risk/(levels*2.5*volatility), cash*5/(levels*entry))/lot_units) * lot_units
                if units >= lot_units:
                    legs = [(side, entry, units)]
                else:
                    rejected += 1
        elif legs and len(legs) < levels and adx[p] is not None and adx[p] < 20:
            side, last_entry, units = legs[-1]
            # Add only on prior closed candle adverse movement, next open execution.
            if side * (bars[p][4] - last_entry) <= -step_atr * volatility:
                entry = op + side * friction
                if (sum(x[2] for x in legs) + units) * entry <= cash * 5:
                    legs.append((side, entry, units))
                else:
                    rejected += 1
        marked = cash + sum(side*units*(close-side*friction-entry) for side,entry,units in legs)
        peak = max(peak, marked)
        dd = max(dd, (peak-marked)/peak)
    if legs:
        close = bars[end-1][4]
        cash += sum(side*units*(close-side*friction-entry) for side,entry,units in legs)
        trades += len(legs)
        peak = max(peak, cash)
        dd = max(dd, (peak-cash)/peak)
    return dict(final=round(cash,2), return_pct=round((cash/initial-1)*100,3), max_drawdown_pct=round(dd*100,3), closed_legs=trades, rejected=rejected)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('csv', type=Path)
    p.add_argument('--initial', type=float, default=1000)
    p.add_argument('--min-lot', type=float, default=.01)
    args = p.parse_args()
    if args.initial <= 0 or args.min_lot <= 0:
        p.error('Initial balance and min lot must be positive')
    bars = load_h4(args.csv)
    split = int(len(bars)*.7)
    print('data_sha256:', hashlib.sha256(args.csv.read_bytes()).hexdigest())
    report = {}
    # Prespecified variants; holdout is reporting only, not a tuning target.
    for levels, step in ((1, 1.0), (2, 1.0), (3, 1.5)):
        name = f'grid_{levels}_step_{step}'
        report[name] = {}
        for label, start, end in (('development',0,split),('holdout',split,len(bars))):
            years = (bars[end-1][0]-bars[start][0]).total_seconds()/(365.25*86400)
            for cost, spread, slip in (('base',1.5,.3),('double',3.0,.6)):
                result = simulate(bars,start,end,args.initial,spread,slip,.005,args.min_lot,levels,step)
                result['annualized_pct'] = round(100*((result['final']/args.initial)**(1/years)-1),3) if result['final']>0 else -100
                report[name][f'{label}_{cost}'] = result
                print(name,label,cost,json.dumps(result,sort_keys=True))
        holdout = report[name]['holdout_base']
        stressed = report[name]['holdout_double']
        passed_gate = holdout['annualized_pct']>10 and stressed['annualized_pct']>10 and holdout['max_drawdown_pct']<=30 and holdout['closed_legs']>=30
        print(name,'TARGET_GT_10_PCT_ANNUAL_GATE:', 'PASS' if passed_gate else 'FAIL')
    Path('reports').mkdir(exist_ok=True)
    Path('reports/bounded_grid_h4.json').write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
    print('RESEARCH ONLY. Bid-only Dukascopy; assumed standard 100k EUR/lot and fixed spread, no swaps, variable costs, broker margin or liquidation. ProCent contract unverified. Data must be pinned before acceptance.')


if __name__ == '__main__':
    main()
