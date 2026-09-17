"""Research-only H4 hybrid; no broker connectivity. All signals use closed candles."""
import argparse
import csv
import math
from pathlib import Path
from trend_h4 import load_h4, indicators, PIP


def run(bars, start, end, initial, spread, slip, risk, min_lot, mode):
    fast, slow, atr, adx = indicators(bars)
    closes = [bar[4] for bar in bars]
    # Bollinger values calculated exclusively from closed bars at signal time.
    balance = peak = initial
    drawdown = 0.0
    position = None
    trades = []
    rejected = 0
    half = spread * PIP / 2
    friction = half + slip * PIP
    for i in range(max(201, start), end):
        timestamp, op, high, low, close = bars[i]
        p = i - 1
        window = closes[p - 19:p + 1]
        middle = sum(window) / 20
        deviation = math.sqrt(sum((v - middle) ** 2 for v in window) / 20)
        upper_bb, lower_bb = middle + 2 * deviation, middle - 2 * deviation
        prev_window = closes[p - 20:p]
        prev_middle = sum(prev_window) / 20
        prev_dev = math.sqrt(sum((v - prev_middle) ** 2 for v in prev_window) / 20)
        prev_upper, prev_lower = prev_middle + 2 * prev_dev, prev_middle - 2 * prev_dev
        if position is not None:
            side, entry, units, stop, opened, strategy = position
            hit = low <= stop if side == 1 else high >= stop
            if strategy == 'trend':
                channel = (closes[p] < min(r[3] for r in bars[i - 11:i - 1]) if side == 1 else closes[p] > max(r[2] for r in bars[i - 11:i - 1]))
                exit_signal = channel
            else:
                exit_signal = closes[p] >= middle if side == 1 else closes[p] <= middle
            if hit or exit_signal:
                # Stop gaps are filled at the worse of open and stop, never at a better price.
                raw = (min(op, stop) if side == 1 else max(op, stop)) if hit else op
                exit_price = raw - side * friction
                pnl = side * units * (exit_price - entry)
                balance += pnl
                trades.append((strategy, opened.isoformat(), timestamp.isoformat(), side, units, round(pnl, 4), round(balance, 4)))
                position = None
        if position is None and balance > 0 and atr[p] and adx[p] is not None:
            side = 0
            strategy = ''
            distance = 0.0
            if mode in ('hybrid', 'trend') and adx[p] > 25:
                channel_high = max(r[2] for r in bars[i - 21:i - 1])
                channel_low = min(r[3] for r in bars[i - 21:i - 1])
                side = 1 if closes[p] > channel_high and fast[p] > slow[p] else -1 if closes[p] < channel_low and fast[p] < slow[p] else 0
                strategy, distance = 'trend', 2 * atr[p]
            elif mode in ('hybrid', 'reversion') and adx[p] < 20:
                # Previous close outside its BB followed by last completed close back inside.
                side = 1 if closes[p - 1] < prev_lower and closes[p] >= lower_bb else -1 if closes[p - 1] > prev_upper and closes[p] <= upper_bb else 0
                strategy, distance = 'reversion', 1.5 * atr[p]
            if side and distance > 0:
                entry = op + side * friction
                step = min_lot * 100000
                units = math.floor(min(balance * risk / distance, balance * 10 / entry) / step + 1e-10) * step
                if units >= step:
                    position = (side, entry, units, entry - side * distance, timestamp, strategy)
                else:
                    rejected += 1
        marked = balance if position is None else balance + position[0] * position[2] * (close - position[1] - position[0] * half)
        peak = max(peak, marked)
        drawdown = max(drawdown, (peak - marked) / peak)
    if position is not None:
        side, entry, units, _, opened, strategy = position
        timestamp, _, _, _, close = bars[end - 1]
        pnl = side * units * (close - side * friction - entry)
        balance += pnl
        trades.append((strategy, opened.isoformat(), timestamp.isoformat(), side, units, round(pnl, 4), round(balance, 4)))
    return {'initial': initial, 'final': round(balance, 2), 'return_pct': round(100 * (balance / initial - 1), 2), 'max_drawdown_pct': round(drawdown * 100, 2), 'trades': len(trades), 'win_rate_pct': round(100 * sum(t[5] > 0 for t in trades) / len(trades), 2) if trades else 0, 'signals_rejected_by_lot_or_risk': rejected}, trades


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('csv', type=Path)
    parser.add_argument('--initial', type=float, default=1000)
    parser.add_argument('--spread-pips', type=float, default=1.5)
    parser.add_argument('--slippage-pips', type=float, default=0.3)
    parser.add_argument('--risk', type=float, default=0.0025)
    parser.add_argument('--min-lot', type=float, default=0.01)
    args = parser.parse_args()
    if args.initial <= 0 or args.spread_pips < 0 or args.slippage_pips < 0 or not 0 < args.risk <= 0.02 or args.min_lot <= 0:
        parser.error('Invalid capital, costs, risk or lot size')
    bars = load_h4(args.csv)
    split = int(len(bars) * .7)
    Path('reports').mkdir(exist_ok=True)
    for mode in ('trend', 'reversion', 'hybrid'):
        for label, start, end in (('development', 0, split), ('holdout', split, len(bars))):
            result, trades = run(bars, start, end, args.initial, args.spread_pips, args.slippage_pips, args.risk, args.min_lot, mode)
            print(mode, label, bars[start][0].isoformat(), bars[end - 1][0].isoformat(), result)
            with open(f'reports/hybrid_{mode}_{label}.csv', 'w', newline='') as handle:
                writer = csv.writer(handle)
                writer.writerow(['strategy', 'entry_utc', 'exit_utc', 'side', 'units_eur', 'pnl_usd', 'balance_usd'])
                writer.writerows(trades)
    print('RESEARCH ONLY: nominal 100k EUR/lot, assumed 10:1 notional cap, fixed spread/slippage; excludes swaps, broker liquidation and variable costs. Verify ProCent contract specification before broker use.')


if __name__ == '__main__':
    main()
