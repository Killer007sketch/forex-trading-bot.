# EUR/USD Forex research bot

Research only. No live orders, MT4 connection or proven profitability.

Python 3.11+, standard library only. Provide a chronological **hourly** EUR/USD CSV at `data/eurusd_h1.csv` with columns `open,high,low,close`. The backtester currently does not validate timestamps, duplicates or missing hours: clean and verify data before use. One-minute files cannot be passed directly without aggregation.

Run tests: `python -m unittest discover -s tests -v`

Run backtest: `python -m src.backtest data/eurusd_h1.csv --initial 300 --spread-pips 1.5 --slippage-pips 0.3`

Output: `reports/trades.csv` and console summary. Baseline: completed-bar 20-bar channel breakout, 10-bar channel exit, 2×ATR(20) fixed stop, 0.5% nominal risk, 10× notional cap, entry on next bar open. Spread and slippage approximated; no swap, commissions, minimum lots, margin liquidation, broker execution constraints or financing. Results are not directly representative of RoboForex ProCent. Full 2016–2026 data has **not** been downloaded or tested yet. Walk-forward/out-of-sample evaluation is required.

GitHub Actions runs unit tests on pushes. It does not download market data or trade.
