"""Research backtest only; no broker connection or live orders."""
import argparse
import csv
from pathlib import Path


def load(path):
    with open(path, newline='', encoding='utf-8-sig') as f:
        rows = []
        for raw in csv.DictReader(f):
            r = {k.strip().lower(): v for k, v in raw.items() if k}
            rows.append(tuple(float(r[k]) for k in ('open', 'high', 'low', 'close')))
    if len(rows) < 55:
        raise ValueError('At least 55 hourly OHLC bars required')
    if any(min(o, h, l, c) <= 0 or h < max(o, l, c) or l > min(o, h, c) for o, h, l, c in rows):
        raise ValueError('Invalid OHLC')
    return rows


def run(rows, initial=300., spread_pips=1.5, slippage_pips=0.3, risk=0.005):
    if initial <= 0 or spread_pips < 0 or slippage_pips < 0 or not 0 < risk <= 0.02:
        raise ValueError('Invalid risk or costs')
    equity, peak, drawdown = initial, initial, 0.
    position, trades = None, []
    cost = (spread_pips / 2 + slippage_pips) * 0.0001
    for i in range(21, len(rows)):
        o, h, l, c = rows[i]
        prev = rows[i-1]
        tr = [max(rows[j][1]-rows[j][2], abs(rows[j][1]-rows[j-1][3]), abs(rows[j][2]-rows[j-1][3])) for j in range(i-20, i)]
        atr = sum(tr)/20
        if position:
            side, entry, units, stop, start = position
            exit_price = None
            if side == 1 and l <= stop:
                exit_price = min(o, stop)-cost
            elif side == -1 and h >= stop:
                exit_price = max(o, stop)+cost
            elif side == 1 and prev[3] < min(r[2] for r in rows[i-11:i-1]):
                exit_price = o-cost
            elif side == -1 and prev[3] > max(r[1] for r in rows[i-11:i-1]):
                exit_price = o+cost
            if exit_price is not None:
                pnl = side*units*(exit_price-entry)
                equity += pnl
                trades.append({'entry_bar': start, 'exit_bar': i, 'side': side, 'pnl_usd': round(pnl, 4), 'equity_usd': round(equity, 4)})
                position = None
        if position is None and equity > 0 and atr > 0:
            upper = max(r[1] for r in rows[i-21:i-1])
            lower = min(r[2] for r in rows[i-21:i-1])
            side = 1 if prev[3] > upper else -1 if prev[3] < lower else 0
            if side:
                distance = 2*atr
                units = min(equity*risk/distance, equity*10/o)
                entry = o+side*cost
                position = (side, entry, units, entry-side*distance, i)
        marked = equity if position is None else equity+position[0]*position[2]*(c-position[1])
        peak = max(peak, marked)
        drawdown = max(drawdown, (peak-marked)/peak)
    if position:
        side, entry, units, _, start = position
        pnl = side*units*(rows[-1][3]-side*cost-entry)
        equity += pnl
        trades.append({'entry_bar': start, 'exit_bar': len(rows)-1, 'side': side, 'pnl_usd': round(pnl, 4), 'equity_usd': round(equity, 4)})
    summary = {'bars': len(rows), 'trades': len(trades), 'initial_usd': initial, 'final_usd': round(equity, 2), 'return_pct': round((equity/initial-1)*100, 2), 'max_drawdown_pct': round(drawdown*100, 2), 'win_rate_pct': round(100*sum(t['pnl_usd'] > 0 for t in trades)/len(trades), 2) if trades else 0}
    return summary, trades


def main():
    p = argparse.ArgumentParser()
    p.add_argument('csv', type=Path)
    p.add_argument('--initial', type=float, default=300)
    p.add_argument('--spread-pips', type=float, default=1.5)
    p.add_argument('--slippage-pips', type=float, default=0.3)
    p.add_argument('--risk', type=float, default=0.005)
    a = p.parse_args()
    summary, trades = run(load(a.csv), a.initial, a.spread_pips, a.slippage_pips, a.risk)
    for key, value in summary.items():
        print(f'{key}: {value}')
    Path('reports').mkdir(exist_ok=True)
    with open('reports/trades.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['entry_bar', 'exit_bar', 'side', 'pnl_usd', 'equity_usd'])
        w.writeheader()
        w.writerows(trades)


if __name__ == '__main__':
    main()
