"""Download and validate Dukascopy EURUSD H1 bid candles for research only.
Dukascopy quotes are not RoboForex executable prices.
"""
import argparse
import csv
import datetime as dt
import math
import subprocess
from pathlib import Path


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
    candidates = sorted((p for p in target.parent.glob('*.csv') if p != target), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise RuntimeError('No source CSV downloaded')
    source = candidates[0]
    print(f'Source: {source}', flush=True)
    with source.open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError('CSV has no header')
        fields = {name.strip().lower(): name for name in reader.fieldnames}
        if not all(k in fields for k in ('open', 'high', 'low', 'close')):
            raise ValueError(f'Unexpected CSV columns: {reader.fieldnames}')
        rows, invalid = [], 0
        for line_number, record in enumerate(reader, start=2):
            try:
                o, h, l, c = (float(record[fields[k]]) for k in ('open', 'high', 'low', 'close'))
                valid = (all(math.isfinite(v) and v > 0 for v in (o, h, l, c))
                         and h >= max(o, l, c) and l <= min(o, h, c))
            except (TypeError, ValueError, KeyError):
                valid = False
            if not valid:
                invalid += 1
                if invalid <= 10:
                    print(f'INVALID source line {line_number}: {record!r}', flush=True)
            else:
                rows.append((o, h, l, c))
    print(f'Validation: {len(rows)} valid, {invalid} invalid candles', flush=True)
    if invalid:
        raise ValueError(f'{invalid} invalid source candles; source line numbers and raw values printed above. No candles silently modified or dropped.')
    if len(rows) < 55:
        raise ValueError(f'Insufficient hourly bars: {len(rows)}')
    with target.open('w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['open', 'high', 'low', 'close'])
        writer.writerows(rows)
    print(f'Validated {len(rows)} bars; wrote {target}', flush=True)


if __name__ == '__main__':
    main()
