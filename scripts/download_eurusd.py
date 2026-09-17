"""Download Dukascopy EURUSD H1 bid candles; audit one-tick rounding fixes.
Research only: Dukascopy quotes are not RoboForex executable prices.
"""
import argparse
import csv
import datetime as dt
import math
import subprocess
from pathlib import Path

TICK = 0.00001


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--from-date', default='2016-01-01')
    p.add_argument('--to-date', default=dt.datetime.now(dt.timezone.utc).date().isoformat())
    p.add_argument('--output', default='data/eurusd_h1.csv')
    args = p.parse_args()
    start, end = dt.date.fromisoformat(args.from_date), dt.date.fromisoformat(args.to_date)
    if start >= end:
        raise ValueError('from-date must precede to-date')
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = ['npx', '--yes', 'dukascopy-node', '-i', 'eurusd', '-from', str(start), '-to', str(end), '-t', 'h1', '-f', 'csv', '-dir', str(target.parent)]
    print('Running:', ' '.join(cmd), flush=True)
    subprocess.run(cmd, check=True)
    candidates = sorted((p for p in target.parent.glob('*.csv') if p != target and p.name != 'ohlc-corrections.csv'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise RuntimeError('No source CSV downloaded')
    source = candidates[0]
    print(f'Source: {source}', flush=True)
    rows, corrections, invalid = [], [], []
    with source.open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError('CSV has no header')
        fields = {name.strip().lower(): name for name in reader.fieldnames}
        if not all(k in fields for k in ('open', 'high', 'low', 'close')):
            raise ValueError(f'Unexpected CSV columns: {reader.fieldnames}')
        time_field = next((fields[k] for k in ('timestamp', 'time', 'datetime', 'date') if k in fields), None)
        if time_field is None:
            raise ValueError(f'No timestamp column in source: {reader.fieldnames}')
        for line_number, record in enumerate(reader, start=2):
            try:
                timestamp = record[time_field].strip()
                if not timestamp:
                    raise ValueError('Empty timestamp')
                o, h, l, c = (float(record[fields[k]]) for k in ('open', 'high', 'low', 'close'))
                if not all(math.isfinite(v) and v > 0 for v in (o, h, l, c)):
                    raise ValueError('Nonpositive or nonfinite price')
                fixed_h, fixed_l = max(o, h, c), min(o, l, c)
                if fixed_h - h > TICK + 1e-10 or l - fixed_l > TICK + 1e-10:
                    raise ValueError('OHLC discrepancy exceeds one tick')
                if fixed_h != h or fixed_l != l:
                    corrections.append((line_number, timestamp, f'{h:.5f}', f'{fixed_h:.5f}', f'{l:.5f}', f'{fixed_l:.5f}'))
                rows.append((timestamp, o, fixed_h, fixed_l, c))
            except (TypeError, ValueError, KeyError) as exc:
                invalid.append((line_number, str(exc), repr(record)))
    audit = target.parent / 'ohlc-corrections.csv'
    with audit.open('w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['source_line', 'timestamp', 'original_high', 'adjusted_high', 'original_low', 'adjusted_low'])
        writer.writerows(corrections)
    print(f'Validation: {len(rows)} accepted, {len(corrections)} one-tick adjustments, {len(invalid)} rejected; audit: {audit}', flush=True)
    for line, reason, record in invalid[:10]:
        print(f'INVALID source line {line}: {reason}: {record}', flush=True)
    if invalid:
        raise ValueError(f'{len(invalid)} invalid source candles; no rows silently dropped')
    if len(rows) < 55:
        raise ValueError(f'Insufficient hourly bars: {len(rows)}')
    with target.open('w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['timestamp', 'open', 'high', 'low', 'close'])
        writer.writerows(rows)
    print(f'Validated {len(rows)} bars; wrote {target}', flush=True)


if __name__ == '__main__':
    main()
