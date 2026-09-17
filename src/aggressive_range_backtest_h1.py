"""RESEARCH ONLY. Aggressive 12h range trades; NEVER sends orders.

One 00 UTC Range/ATR<5 opportunity per day, 80 completed H1 extremes frozen
for 12h. Previous hourly test state: on boundary wick pause immediately;
rearm only after a FULL contained closed H1 and fresh ATR ratio<5, for NEXT
hour. Ratio failure at close pauses next hour. No intrabar execution chronology:
market entry at known open, adverse stop before take profit if both touched.
BID-only H1, hypothetical fixed bid-ask spread and adverse slippage, neither
live executable quotes nor accurate intrahour pause latency. Existing positions
retain protective stops/TP through a pause; forced exit at cycle expiry.
Historical 2023-26 was repeatedly inspected, NOT fresh out-of-sample.
"""
import csv
import datetime as dt
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path

from compare_tpo_atr_h1 import HORIZON, collect, range_atr_signal, simple_atr
from range_guard_h1 import execution, load_h1
from trend_h4 import PIP, indicators, load_h4

INITIAL_USD = 10000.0
RISK_LEVELS = {'comparison_0p5pct': 0.005, 'aggressive_1p5pct': 0.015}
COSTS = {'base': (1.5, 0.3), 'double': (3.0, 0.6)}
MAX_NOTIONAL_TO_EQUITY = 10.0  # simulated cap only, NOT authorized leverage
MAX_REALIZED_SESSION_LOSS = .03
MAX_TRADES_PER_CYCLE = 3
ENTRY_FRACTION = .30  # near lower/upper 30% of frozen 80h range
TARGET_FRACTION = .70  # long 70%, short 30% of frozen 80h range
LOT_UNITS = 1000


def trading_cycle(window, ratios, lower, upper, balance, peak, max_dd, risk, spread, slip):
    """One 12h cycle. All future H1 data consumed one candle at a time.

    Entry at H1 open only when already armed; stop checked before TP, including
    both-touched candles. Boundary crossing hour is never claimed wholly safe.
    A carried position has exits even when new entries are prohibited.
    """
    if len(window) != HORIZON or len(ratios) != HORIZON or not lower < upper:
        raise ValueError('Invalid frozen 12h cycle')
    width = upper-lower
    spread_px = spread*PIP
    active = True
    position = None
    session_start = balance
    entries = 0
    events = {'stops': 0, 'targets': 0, 'expiry': 0, 'breach_hours': 0,
              'pause_transitions': 0, 'resume_transitions': 0, 'ratio_pauses': 0,
              'skipped_small_stop': 0, 'skipped_size': 0, 'max_session_loss_block': 0}
    trades = []
    def exit_trade(bid, reason, at):
        nonlocal balance, position, peak, max_dd
        p = position
        side, units, entry, opened = p['side'], p['units'], p['entry'], p['opened']
        fill = execution(bid, -side, spread, slip)
        pnl = side*units*(fill-entry)
        balance += pnl
        peak = max(peak, balance)
        max_dd = max(max_dd, (peak-balance)/peak)
        trades.append({'entry_utc':opened.isoformat(), 'exit_utc':at.isoformat(),
                       'side':'long' if side == 1 else 'short', 'units_eur':units,
                       'entry_quote':round(entry,6), 'exit_quote':round(fill,6),
                       'pnl_usd':round(pnl,5), 'exit_reason':reason})
        events[reason] += 1
        position = None
    for j, (t, op, high, low, close) in enumerate(window):
        # Only history completed before this open may size a newly entered trade.
        atr_at_open = ratios[j][1]
        if position is None and active and lower <= op <= upper and entries < MAX_TRADES_PER_CYCLE:
            if balance-session_start <= -MAX_REALIZED_SESSION_LOSS*session_start:
                events['max_session_loss_block'] += 1
            else:
                side = 1 if op <= lower+ENTRY_FRACTION*width else -1 if op >= upper-ENTRY_FRACTION*width else 0
                if side:
                    entry = execution(op, side, spread, slip)
                    # For a short, protective stop triggers on ASK >= upper.
                    stop_bid = lower if side == 1 else upper-spread_px
                    exit_at_stop = execution(stop_bid, -side, spread, slip)
                    risk_per_unit = side*(entry-exit_at_stop)
                    target_bid = lower+(TARGET_FRACTION if side == 1 else 1-TARGET_FRACTION)*width
                    exit_at_target = execution(target_bid, -side, spread, slip)
                    reward_per_unit = side*(exit_at_target-entry)
                    if (atr_at_open is None or risk_per_unit < atr_at_open or
                            reward_per_unit <= 1.25*risk_per_unit):
                        events['skipped_small_stop'] += 1
                    else:
                        units_by_risk = math.floor((balance*risk/risk_per_unit)/LOT_UNITS)*LOT_UNITS
                        units_by_notional = math.floor((balance*MAX_NOTIONAL_TO_EQUITY/entry)/LOT_UNITS)*LOT_UNITS
                        units = int(min(units_by_risk, units_by_notional))
                        if units < LOT_UNITS:
                            events['skipped_size'] += 1
                        else:
                            position = {'side':side,'units':units,'entry':entry,'opened':t}
                            entries += 1
        # H1 OHLC has unknown event order: adverse stop gets priority over TP.
        if position is not None:
            side = position['side']
            stop_bid = lower if side == 1 else upper-spread_px
            hit_stop = low <= stop_bid if side == 1 else high >= stop_bid
            tp_bid = lower+(TARGET_FRACTION if side == 1 else 1-TARGET_FRACTION)*width
            hit_tp = high >= tp_bid if side == 1 else low <= tp_bid
            worst_bid = min(op,low) if side == 1 else max(op,high)
            mark = balance+side*position['units']*(execution(worst_bid,-side,spread,slip)-position['entry'])
            max_dd = max(max_dd,(peak-mark)/peak)
            if hit_stop:
                bid = min(op,stop_bid) if side == 1 else max(op,stop_bid)
                exit_trade(bid,'stops',t+dt.timedelta(hours=1))
            elif hit_tp:
                exit_trade(tp_bid,'targets',t+dt.timedelta(hours=1))
        breach = high > upper or low < lower
        if breach:
            events['breach_hours'] += 1
            if active:
                events['pause_transitions'] += 1
            active = False
        elif active:
            # Hour-j ATR only known after close, and applied from next hour.
            if ratios[j][0] is None or ratios[j][0] >= 5.0:
                events['ratio_pauses'] += 1
                active = False
        elif ratios[j][0] is not None and ratios[j][0] < 5.0 and j < HORIZON-1:
            active = True
            events['resume_transitions'] += 1
    if position is not None:
        exit_trade(window[-1][4],'expiry',window[-1][0]+dt.timedelta(hours=1))
    assert position is None and entries <= MAX_TRADES_PER_CYCLE
    return balance,peak,max_dd,trades,events


def run_segment(h1, episodes, risk, spread, slip):
    by_time = {bar[0]:i for i,bar in enumerate(h1)}
    balance=peak=INITIAL_USD
    max_dd=0.0
    all_trades=[]
    tallies={}
    active_cycles=0
    for episode in episodes:
        if not episode['range_atr']:
            continue
        i=by_time[episode['time']]
        future=h1[i:i+HORIZON]
        ratios=[]
        for j in range(HORIZON):
            # At open j previous close is i+j-1. At close j history includes i+j.
            before=h1[i-80:i+j]
            finished=h1[i-80:i+j+1]
            ratios.append((range_atr_signal(finished),simple_atr(before)))
        balance,peak,max_dd,trades,events=trading_cycle(
            future,ratios,episode['lower'],episode['upper'],balance,peak,max_dd,risk,spread,slip)
        active_cycles += bool(trades)
        all_trades.extend([{'cycle_utc':episode['time'].isoformat(),**x} for x in trades])
        for key,value in events.items():
            tallies[key]=tallies.get(key,0)+value
    wins=[t['pnl_usd'] for t in all_trades if t['pnl_usd']>0]
    losses=[-t['pnl_usd'] for t in all_trades if t['pnl_usd']<0]
    return ({'initial_usd':INITIAL_USD,'final_usd':round(balance,2),
             'net_pnl_usd':round(balance-INITIAL_USD,2),
             'return_pct':round((balance/INITIAL_USD-1)*100,2),
             'max_intrabar_worst_drawdown_pct':round(max_dd*100,2),
             'signals':sum(e['range_atr'] for e in episodes),
             'cycles_with_trades':active_cycles,'closed_trades':len(all_trades),
             'wins':len(wins),'losses':len(losses),
             'win_rate_pct':round(len(wins)/len(all_trades)*100,2) if all_trades else None,
             'profit_factor':round(sum(wins)/sum(losses),3) if losses else None,
             'average_trade_usd':round((balance-INITIAL_USD)/len(all_trades),2) if all_trades else None,
             'mean_win_usd':round(statistics.mean(wins),2) if wins else None,
             'mean_loss_usd':round(statistics.mean(losses),2) if losses else None,
             'events':tallies},all_trades)


def main():
    if len(sys.argv)!=2:
        raise SystemExit('Usage: python src/aggressive_range_backtest_h1.py data/eurusd_h1.csv')
    path=Path(sys.argv[1]); h1=load_h1(path);h4=load_h4(path)
    _,_,h4atr,_=indicators(h4)
    split=int(len(h4)*.7)
    split_time=h4[split][0]
    end=h4[-1][0]+dt.timedelta(hours=4)
    report={'data_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'parameters':{'initial_usd':INITIAL_USD,'risk_levels':RISK_LEVELS,'costs_pips_spread_slip_per_fill':COSTS,
                          'risk_limit_per_cycle_realized_pct':MAX_REALIZED_SESSION_LOSS*100,
                          'max_hypothetical_notional_to_equity':MAX_NOTIONAL_TO_EQUITY,
                          'max_entries_per_cycle':MAX_TRADES_PER_CYCLE,
                          'frozen_80h_channel_entry_zone_fraction':ENTRY_FRACTION,
                          'take_profit_fraction_long':TARGET_FRACTION,
                          'take_profit_fraction_short':1-TARGET_FRACTION,
                          'stop':'frozen outer boundary; short ASK-adjusted; gaps adverse',
                          'intrabar_priority':'stop before target whenever both in same H1',
                          'pause':'on outer wick or ATR failure; pending position retains stop and TP',
                          'rearm':'complete inner H1 and ATR<5 at its close; next hour only',
                          'cycle_expiry':'exit any remaining position after twelfth close'},
            'limitations':'Research only, NOT live trades. Dukascopy BID H1; fixed hypothetical spread/slippage with 2x stress but no real broker fees, variable news spreads, swaps, latency or tick order. 10x notional cap is a simulated mathematical exposure limit, NOT margin or broker model. Unknown H1 crossing chronology resolved adverse-first; same-hour entry/breach is assumed filled before boundary stop; actual boundary pause latency unobservable. Intrabar low/high mark-to-market drawdown is conservative and may exceed realized stop. One midnight sample each day, 12h no overlapping sessions. Prior 2023-26 was repeatedly inspected, NOT independent holdout. Parameters declared before running this study, NOT optimized on these results.',
            'segments':{}}
    Path('reports').mkdir(exist_ok=True)
    for name,begin,finish in (('development',h4[200][0],split_time),('previously_inspected_2023_2026',split_time,end)):
        episodes,audit=collect(h1,h4,h4atr,begin,finish)
        result={'audit':audit,'variants':{}}
        for risk_name,risk in RISK_LEVELS.items():
            for cost_name,(spread,slip) in COSTS.items():
                metric,trades=run_segment(h1,episodes,risk,spread,slip)
                key=f'{risk_name}_{cost_name}'
                result['variants'][key]=metric
                with (Path('reports')/f'aggressive_range_{name}_{key}.csv').open('w',newline='') as f:
                    fields=['cycle_utc','entry_utc','exit_utc','side','units_eur','entry_quote','exit_quote','pnl_usd','exit_reason']
                    writer=csv.DictWriter(f,fieldnames=fields)
                    writer.writeheader();writer.writerows(trades)
        report['segments'][name]=result
        print(name,json.dumps(result,sort_keys=True))
    (Path('reports')/'aggressive_range_backtest.json').write_text(json.dumps(report,indent=2)+'\n')
    print('RESEARCH ONLY. NO BROKER CONNECTION. No claim of profitable live strategy.')


if __name__=='__main__':
    main()
