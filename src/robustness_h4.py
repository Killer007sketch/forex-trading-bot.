"""Research-only robustness audit. No optimization on the holdout and no orders."""
import argparse
import hashlib
import json
import math
from pathlib import Path
from hybrid_h4 import run
from trend_h4 import load_h4


def main():
    p = argparse.ArgumentParser()
    p.add_argument('csv', type=Path)
    p.add_argument('--initial', type=float, default=1000)
    p.add_argument('--risk', type=float, default=0.0025)
    p.add_argument('--min-lot', type=float, default=0.01)
    a = p.parse_args()
    if a.initial <= 0 or not 0 < a.risk <= 0.02 or a.min_lot <= 0:
        p.error('Invalid capital, risk or minimum lot')
    bars = load_h4(a.csv)
    split = int(len(bars) * 0.7)
    print('data_sha256:', hashlib.sha256(a.csv.read_bytes()).hexdigest())
    print('h4_bars:', len(bars), 'development_end:', bars[split-1][0].isoformat(), 'holdout_start:', bars[split][0].isoformat())
    report = {}
    for mode in ('trend', 'reversion', 'hybrid'):
        report[mode] = {}
        for spread, slip, label in ((1.5, 0.3, 'base'), (3.0, 0.6, 'double_costs')):
            for period, start, end in (('development', 0, split), ('holdout', split, len(bars))):
                metrics, trades = run(bars, start, end, a.initial, spread, slip, a.risk, a.min_lot, mode)
                years = (bars[end-1][0] - bars[start][0]).total_seconds() / (365.25 * 86400)
                cagr = 100 * ((metrics['final'] / a.initial) ** (1 / years) - 1) if metrics['final'] > 0 and years > 0 else -100.0
                metrics['annualized_pct'] = round(cagr, 3)
                metrics['years'] = round(years, 3)
                report[mode][f'{period}_{label}'] = metrics
                print(mode, period, label, json.dumps(metrics, sort_keys=True))
        h = report[mode]['holdout_base']
        stressed = report[mode]['holdout_double_costs']
        passed = h['annualized_pct'] > 10 and stressed['annualized_pct'] > 10 and h['trades'] >= 30 and h['max_drawdown_pct'] <= 30
        print(mode, 'TARGET_GT_10_PCT_ANNUAL_GATE:', 'PASS' if passed else 'FAIL', '(not a guarantee of future returns)')
    Path('reports').mkdir(exist_ok=True)
    Path('reports/robustness_h4.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print('Research limitations: bid-only Dukascopy data; unverified RoboForex ProCent contract size, margin and minimum lot; fixed spreads; no swaps; changing historical downloads must be pinned before acceptance.')


if __name__ == '__main__':
    main()
