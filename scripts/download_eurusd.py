"""Download public Dukascopy EUR/USD hourly candles via dukascopy-node CLI.
Research only. Dukascopy quotes are not RoboForex execution prices.
"""
import argparse
import csv
import datetime as dt
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
    # CLI options are checked by the workflow's help command before invocation.
    cmd = ['npx', '--yes', 'dukascopy-node', '-i', 'eurusd', '-from', str(start), '-to', str(end), '-t', 'h1', '-f', 'csv', '-dir', str(target.parent)]
    print('Running:', ' '.join(cmd), flush=True)
    subprocess.run(cmd, check=True)
    candidates = sorted(target.parent.glob('*.csv'), key=lambda x: x.stat().st_mtime, reverse=True)
    if not candidates:
        raise RuntimeError('No CSV downloaded; inspect dukascopy-node CLI output')
    source = candidates[0]
    with source.open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError('CSV has no header')
        fields = {name.strip().lower(): name for name in reader.fieldnames}
        if not all(k in fields for k in ('open', 'high', 'low', 'close')):
            raise ValueError(f'Unexpected Dukascopy CSV columns: {reader.fieldnames}')
        rows = []
        for record in reader:
            try:
                values = [float(record[fields[k]]) for k in ('open', 'high', 'low', 'close')]
            except (TypeError, ValueError) as exc:
                raise ValueError('Non-numeric OHLC in source CSV') from exc
            if min(values) <= 0 or values[1] < max(values[0], values[3]) or values[2] > min(values[0], values[3]):
                raise ValueError('Invalid OHLC in source CSV')
            rows.append(values)
    if len(rows) < 55:
        raise ValueError(f'Insufficient hourly bars: {len(rows)}')
    with target.open('w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['open', 'high', 'low', 'close'])
        writer.writerows(rows)
    print(f'Validated {len(rows)} bars; wrote {target}')


if __name__ == '__main__':
    main()
