"""Research only. H4 trend hypothesis; no broker connection or live orders.

Requires timestamped H1 CSV; does not fabricate H4 timestamps or fill missing bars.
"""
import argparse
import csv
import datetime as dt
import math
from pathlib import Path

PIP = 0.0001


def parse_time(value):
    value = str(value).strip()
    if value.isdigit():
        number = int(value)
        return dt.datetime.fromtimestamp(number / (1000 if number > 10**11 else 1), dt.timezone.utc)
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timestamps must specify timezone (UTC)')
    return parsed.astimezone(dt.timezone.utc)


def load_h4(path):
    buckets = {}
    last = None
    with open(path, newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        names = {x.strip().lower(): x for x in (reader.fieldnames or [])}
        time_key = next((names[x] for x in ('timestamp', 'time', 'datetime', 'date') if x in names), None)
        if time_key is None:
            raise ValueError('Timestamped H1 data required; downloader must preserve UTC timestamp')
        for number, row in enumerate(reader, 2):
            timestamp = parse_time(row[time_key])
            if timestamp.minute or timestamp.second or timestamp.microsecond or (last is not None and timestamp <= last):
                raise ValueError(f'Non-hourly or non-increasing timestamp at line {number}')
            last = timestamp
            o, h, l, c = (float(row[names[k]]) for k in ('open', 'high', 'low', 'close'))
            if not all(math.isfinite(v) and v > 0 for v in (o, h, l, c)) or h < max(o, l, c) or l > min(o, h, c):
                raise ValueError(f'Invalid OHLC at line {number}')
            key = timestamp.replace(hour=timestamp.hour // 4 * 4)
            if key not in buckets:
                buckets[key] = [o, h, l, c, 0]
            b = buckets[key]
            b[1], b[2], b[3], b[4] = max(b[1], h), min(b[2], l), c, b[4] + 1
    # Do not silently turn partial 4-hour candles into complete candles.
    candles = [(t, *v[:4]) for t, v in sorted(buckets.items()) if v[4] == 4]
    partial = sum(v[4] != 4 for v in buckets.values())
    print(f'H4 aggregation: {len(candles)} complete, {partial} incomplete buckets excluded')
    if len(candles) < 250:
        raise ValueError('Not enough complete H4 candles')
    return candles


def ema(values, period):
    output = []
    current = None
    alpha = 2 / (period + 1)
    for value in values:
        current = value if current is None else current + alpha * (value - current)
        output.append(current)
    return output


def indicators(bars):
    closes = [r[4] for r in bars]
    fast, slow = ema(closes, 50), ema(closes, 200)
    trs, plus, minus = [], [], []
    for i, (_, o, h, l, c) in enumerate(bars):
        if not i:
            trs.append(h - l); plus.append(0); minus.append(0)
        else:
            ph, pl, pc = bars[i - 1][2:5]
            up, down = h - ph, pl - l
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
            plus.append(up if up > down and up > 0 else 0)
            minus.append(down if down > up and down > 0 else 0)
    atr, adx = [None] * len(bars), [None] * len(bars)
    if len(bars) <= 28:
        return fast, slow, atr, adx
    smtr, smplus, smminus = sum(trs[1:15]), sum(plus[1:15]), sum(minus[1:15])
    dx = []
    for i in range(14, len(bars)):
        if i > 14:
            smtr += trs[i] - smtr / 14
            smplus += plus[i] - smplus / 14
            smminus += minus[i] - smminus / 14
        atr[i] = smtr / 14
        denominator = smplus + smminus
        dx.append(100 * abs(smplus - smminus) / denominator if denominator else 0)
        if len(dx) == 14:
            adx[i] = sum(dx) / 14
        elif len(dx) > 14:
            adx[i] = (adx[i - 1] * 13 + dx[-1]) / 14
    return fast, slow, atr, adx


def simulate(bars, start, end, initial, spread, slip, risk, min_lot):
    fast, slow, atr, adx = indicators(bars)
    balance, peak, max_dd, position, trades = initial, initial, 0., None, []
    half_spread = spread * PIP / 2
    slippage = slip * PIP
    for i in range(max(201, start), end):
        t, o, h, l, c = bars[i]
        previous = i - 1
        if position is not None:
            side, entry, units, stop, opened = position
            channel_exit = (side == 1 and bars[previous][4] < min(r[3] for r in bars[i - 11:i - 1])) or (side == -1 and bars[previous][4] > max(r[2] for r in bars[i - 11:i - 1]))
            hit = (side == 1 and l <= stop) or (side == -1 and h >= stop)
            if hit or channel_exit:
                bid_ask_exit = (min(o, stop) if side == 1 else max(o, stop)) if hit else o
                exit_price = bid_ask_exit - side * (half_spread + slippage)
                pnl = side * units * (exit_price - entry)
                balance += pnl
                trades.append((opened.isoformat(), t.isoformat(), side, units, pnl, balance))
                position = None
        if position is None and balance > 0 and atr[previous] and adx[previous] is not None and adx[previous] > 25:
            close = bars[previous][4]
            upper = max(r[2] for r in bars[i - 21:i - 1])
            lower = min(r[3] for r in bars[i - 21:i - 1])
            side = 1 if close > upper and fast[previous] > slow[previous] else -1 if close < lower and fast[previous] < slow[previous] else 0
            if side:
                entry = o + side * (half_spread + slippage)
                distance = 2 * atr[previous]
                # No fractional broker lots: EURUSD standard lot = 100,000 EUR.
                units = math.floor(min(balance * risk / distance, balance * 10 / entry) / (min_lot * 100000)) * (min_lot * 100000)
                if units > 0:
                    position = (side, entry, units, entry - side * distance, t)
        marked = balance if position is None else balance + position[0] * position[2] * (c - position[1] - position[0] * half_spread)
        peak = max(peak, marked)
        max_dd = max(max_dd, (peak - marked) / peak)
    if position is not None:
        side, entry, units, _, opened = position
        t, _, _, _, close = bars[end - 1]
        pnl = side * units * (close - side * (half_spread + slippage) - entry)
        balance += pnl
        trades.append((opened.isoformat(), t.isoformat(), side, units, pnl, balance))
    return {'initial': initial, 'final': round(balance, 2), 'return_pct': round((balance / initial - 1) * 100, 2), 'max_drawdown_pct': round(max_dd * 100, 2), 'trades': len(trades), 'win_rate_pct': round(100 * sum(x[4] > 0 for x in trades) / len(trades), 2) if trades else 0}, trades


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('csv', type=Path)
    parser.add_argument('--initial', type=float, default=300)
    parser.add_argument('--spread-pips', type=float, default=1.5)
    parser.add_argument('--slippage-pips', type=float, default=0.3)
    parser.add_argument('--risk', type=float, default=0.0025)
    parser.add_argument('--min-lot', type=float, default=0.01)
    args = parser.parse_args()
    if args.initial <= 0 or args.spread_pips < 0 or args.slippage_pips < 0 or not 0 < args.risk <= 0.02 or args.min_lot <= 0:
        parser.error('Invalid capital, costs, risk or lot size')
    bars = load_h4(args.csv)
    split = int(len(bars) * 0.7)
    for name, start, end in [('development', 0, split), ('holdout', split, len(bars))]:
        result, trades = simulate(bars, start, end, args.initial, args.spread_pips, args.slippage_pips, args.risk, args.min_lot)
        print(name, bars[start][0].isoformat(), bars[end - 1][0].isoformat(), result)
        Path('reports').mkdir(exist_ok=True)
        with open(f'reports/trend_h4_{name}.csv', 'w', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['entry_utc', 'exit_utc', 'side', 'units_eur', 'pnl_usd', 'equity_usd'])
            writer.writerows(trades)
    print('RESEARCH ONLY: ignores swaps, broker margin liquidation and changing spreads; holdout is chronological, not walk-forward.')


if __name__ == '__main__':
    main()
