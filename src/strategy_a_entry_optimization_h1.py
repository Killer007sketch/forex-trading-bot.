"""Offline research ONLY. Test entry filters, keep original Strategy A exits and risk.

Each observation uses only completed hourly candles. A candidate enters at the
NEXT H1 open after a contained prior-candle rejection. The 12h clock is not
reset and prior-80h boundaries remain frozen. No broker orders.
"""
import csv
import datetime as dt
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path

import strategy_a_no_hedge_trailing_h1 as base
from compare_tpo_atr_h1 import HORIZON, collect, range_atr_signal, simple_atr
from range_guard_h1 import execution, load_h1
from trend_h4 import PIP, indicators, load_h4

# Small predeclared grid: edge-only controls plus various CONFIRMED reversals.
MODES = {
    'original_edge30': ('edge', .30, 0., 0.),
    'edge10': ('edge', .10, 0., 0.),
    'edge20': ('edge', .20, 0., 0.),
    'edge40': ('edge', .40, 0., 0.),
    'rebound10': ('rebound', .40, .10, .08),
    'rebound20': ('rebound', .45, .20, .10),
    'rebound30': ('rebound', .45, .30, .15),
    'rebound20_strong': ('strong', .45, .20, .10),
    'rebound20_twoclose': ('two', .45, .20, .10),
}


def choose_entry(mode, bars, j, lower, upper):
    style, band, touch, bounce = MODES[mode]
    op = bars[j][1]
    width = upper-lower
    if style == 'edge':
        return 1 if op <= lower+band*width else -1 if op >= upper-band*width else 0
    if j == 0:  # no prior candle INSIDE this defined 12h regime
        return 0
    prev = bars[j-1]
    _t, po, ph, pl, pc = prev
    if not lower <= pl <= ph <= upper:  # reject breached or invalid channel
        return 0
    long = (lower <= op <= lower+band*width and
            pl <= lower+touch*width and pc >= lower+bounce*width and pc > po)
    short = (upper-band*width <= op <= upper and
             ph >= upper-touch*width and pc <= upper-bounce*width and pc < po)
    if style == 'strong':
        span = ph-pl
        long = long and span > 0 and (pc-pl)/span >= .70
        short = short and span > 0 and (ph-pc)/span >= .70
    if style == 'two':
        if j < 2:
            return 0
        before = bars[j-2]
        long = long and before[4] < pc and before[4] > before[1]
        short = short and before[4] > pc and before[4] < before[1]
    return 1 if long else -1 if short else 0


def variant_cycle(bars, historic, lower, upper, balance, peak, dd, spread, slip, mode):
    if len(bars) != HORIZON or len(historic) != HORIZON:
        raise ValueError('12 bars required')
    width = upper-lower
    session_start = balance
    armed = True
    pos = None
    entered = 0
    counts = Counter()
    trades = []

    def mark(bid):
        nonlocal dd
        value = (balance if pos is None else balance + pos['side']*pos['units'] *
                 (execution(bid,-pos['side'],spread,slip)-pos['entry']))
        if peak > 0:
            dd=max(dd,(peak-value)/peak)

    def close(bid, reason, timestamp):
        nonlocal pos, balance, peak
        p=pos
        fill=execution(bid,-p['side'],spread,slip)
        pnl=p['side']*p['units']*(fill-p['entry'])
        balance+=pnl
        trades.append({'opened_utc':p['opened'].isoformat(),
            'closed_utc':timestamp.isoformat(), 'side':'long' if p['side']==1 else 'short',
            'entry_price':round(p['entry'],7),'exit_price':round(fill,7),
            'units_eur':p['units'],'pnl_usd':round(pnl,5),
            'reason':reason,'trailing_was_armed':p['armed']})
        counts[reason]+=1
        pos=None
        peak=max(peak,balance)
        mark(bid)

    for j,(time,op,hi,lo,cl) in enumerate(bars):
        finish=time+dt.timedelta(hours=1)
        if pos is None and armed and lower <= op <= upper and entered < base.MAX_TRADES_PER_CYCLE:
            if balance-session_start <= -.03*session_start:
                counts['session_block']+=1
            else:
                side=choose_entry(mode,bars,j,lower,upper)
                if side:
                    entry=execution(op,side,spread,slip)
                    boundary=base.stop_bid(side,lower,upper,spread)
                    stopped=execution(boundary,-side,spread,slip)
                    risk_per_unit=side*(entry-stopped)
                    activation=base.target_bid(side,lower,upper)
                    reward=side*(execution(activation,-side,spread,slip)-entry)
                    atr=historic[j][1]
                    if atr is None or risk_per_unit<=0 or risk_per_unit<atr or reward<=0:
                        counts['skipped_signal']+=1
                    else:
                        by_risk=math.floor(balance*base.RISK/risk_per_unit/base.LOT_UNITS)*base.LOT_UNITS
                        by_cap=math.floor(balance*base.NOTIONAL_CAP/entry/base.LOT_UNITS)*base.LOT_UNITS
                        units=int(min(by_risk,by_cap))
                        if units<base.LOT_UNITS:
                            counts['skipped_size']+=1
                        else:
                            pos={'opened':time,'side':side,'units':units,
                                 'entry':entry,'armed':False,'trail':None}
                            entered+=1
                            counts['entries']+=1
                            mark(op)

        if pos is not None:
            side=pos['side']
            outer=base.stop_bid(side,lower,upper,spread)
            activation=base.target_bid(side,lower,upper)
            hit_outer=lo<=outer if side==1 else hi>=outer
            hit_activation=hi>=activation if side==1 else lo<=activation
            if hit_outer and hit_activation and not pos['armed']:
                counts['stop_and_activation_same_h1']+=1
            if pos['armed']:
                trail=pos['trail']
                hit_trail=lo<=trail if side==1 else hi>=trail
                if hit_trail and hit_outer:
                    counts['trailing_and_boundary_same_h1']+=1
                if hit_trail:
                    bid=min(op,trail) if side==1 else max(op,trail)
                    if op<trail if side==1 else op>trail:
                        counts['trailing_gaps']+=1
                    mark(bid)
                    close(bid,'trailing_exits',finish)
                elif hit_outer:
                    bid=min(op,outer) if side==1 else max(op,outer)
                    mark(bid)
                    close(bid,'stops',finish)
            elif hit_outer:
                bid=min(op,outer) if side==1 else max(op,outer)
                if op<outer if side==1 else op>outer:
                    counts['stop_gaps']+=1
                mark(bid)
                close(bid,'stops',finish)
            else:
                mark(lo if side==1 else hi)
                if cl>=activation if side==1 else cl<=activation:
                    if not pos['armed']:
                        pos['armed']=True
                        counts['trailing_armed']+=1
                    new=max(activation,cl-base.TRAIL_GAP*width) if side==1 else min(activation,cl+base.TRAIL_GAP*width)
                    if pos['trail'] is None:
                        pos['trail']=new
                    else:
                        pos['trail']=max(pos['trail'],new) if side==1 else min(pos['trail'],new)
            if pos is not None:
                mark(cl)

        outside=lo<lower or hi>upper
        if outside:
            if armed:
                counts['boundary_pauses']+=1
            armed=False
        elif armed:
            if historic[j][0] is None or historic[j][0]>=5:
                counts['atr_pauses']+=1
                armed=False
        elif historic[j][0] is not None and historic[j][0]<5 and j<HORIZON-1:
            armed=True
            counts['resumes']+=1
    if pos is not None:
        close(bars[-1][4],'expiries',bars[-1][0]+dt.timedelta(hours=1))
    assert pos is None and entered<=base.MAX_TRADES_PER_CYCLE
    return balance,peak,dd,counts,trades


def evaluate_cached(cases,mode,spread,slip):
    balance=peak=base.INITIAL_USD
    dd=0.
    total=Counter()
    records=[]
    for ep,bars,historic in cases:
        if mode=='original_edge30':
            balance,peak,dd,ev,trades,_=base.simulate_cycle(
                bars,historic,ep['lower'],ep['upper'],balance,peak,dd,spread,slip,True)
        else:
            balance,peak,dd,ev,trades=variant_cycle(
                bars,historic,ep['lower'],ep['upper'],balance,peak,dd,spread,slip,mode)
        total.update(ev)
        records.extend({'cycle_utc':ep['time'].isoformat(),**t} for t in trades)
    breakdown={}
    for reason in ('stops','trailing_exits','expiries'):
        group=[x for x in records if x['reason']==reason]
        breakdown[reason]={'n':len(group),'net_usd':round(sum(x['pnl_usd'] for x in group),2),
                           'positive':sum(x['pnl_usd']>0 for x in group),
                           'negative':sum(x['pnl_usd']<0 for x in group)}
    return {'initial_usd':base.INITIAL_USD,'final_usd':round(balance,2),
        'return_pct':round(100*(balance/base.INITIAL_USD-1),2),
        'drawdown_pct':round(100*dd,2),'flat_cycles':len(cases),
        'trades':len(records),'wins':sum(x['pnl_usd']>0 for x in records),
        'losses':sum(x['pnl_usd']<0 for x in records),
        'breakdown':breakdown,'events':dict(total)},records


def self_test():
    t=dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)
    def bar(i,o,h,l,c):return (t+dt.timedelta(hours=i),o,h,l,c)
    data=[bar(0,1.10,1.14,1.03,1.13),bar(1,1.13,1.16,1.09,1.15)]
    assert choose_entry('rebound20',data,1,1.,2.)==1
    assert choose_entry('rebound20',data,0,1.,2.)==0
    data=[bar(0,1.90,1.98,1.85,1.86),bar(1,1.86,1.90,1.84,1.85)]
    assert choose_entry('rebound20',data,1,1.,2.)==-1
    data=[bar(0,1.1,1.14,.99,1.13),bar(1,1.13,1.16,1.09,1.15)]
    assert choose_entry('rebound20',data,1,1.,2.)==0
    # The duplicate simulator must match the canonical baseline exactly.
    series=[bar(i,1.10,1.2,1.04,1.13) for i in range(12)]
    past=[(4.,.01)]*12
    actual=base.simulate_cycle(series,past,1.,2.,10000.,10000.,0.,1.5,.3,True)
    other=variant_cycle(series,past,1.,2.,10000.,10000.,0.,1.5,.3,'original_edge30')
    assert actual[0:3]==other[0:3] and actual[4]==other[4],(actual,other)
    print('Entry confirmation tests and identical baseline simulator: PASS')


def main():
    if len(sys.argv)==2 and sys.argv[1]=='--self-test':
        self_test();return
    if len(sys.argv)!=2:
        raise SystemExit('Usage: script data/eurusd_h1.csv')
    path=Path(sys.argv[1]);h1=load_h1(path);h4=load_h4(path)
    _,_,h4atr,_=indicators(h4)
    split=int(len(h4)*.7)
    periods=[('development',h4[200][0],h4[split][0]),
             ('previously_inspected_2023_2026',h4[split][0],h4[-1][0]+dt.timedelta(hours=4))]
    report={'data_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'method':'Frozen 80-hour range, same Range/ATR<5, same 12h clock/stop/closed-H1 52% activation/2%-width trailing, 1.5%-reference sizing and 4x notional cap. Only entry rule varies.',
        'modes':MODES,'costs':base.COSTS,
        'limitations':'Research only. No orders; BID H1, assumed spread/slippage, not actual intrahour prices or real ASK; no fees/swaps/margin. 2023-2026 already repeatedly inspected, NOT fresh OOS. Multiple comparisons create selection bias. Rejection must be observed in prior completed bar and entry is at next open; no lookahead.',
        'segments':{}}
    Path('reports').mkdir(exist_ok=True)
    lookup={row[0]:i for i,row in enumerate(h1)}
    for label,beg,end in periods:
        episodes,audit=collect(h1,h4,h4atr,beg,end)
        cases=[]
        for ep in episodes:
            if not ep['range_atr']:
                continue
            i=lookup[ep['time']]
            historic=[(range_atr_signal(h1[i-80:i+j+1]),simple_atr(h1[i-80:i+j])) for j in range(HORIZON)]
            cases.append((ep,h1[i:i+HORIZON],historic))
        out={'audit':audit,'variants':{}}
        for mode in MODES:
            for cost,(spread,slip) in base.COSTS.items():
                stats,records=evaluate_cached(cases,mode,spread,slip)
                out['variants'][f'{mode}_{cost}']=stats
                print(label,mode,cost,json.dumps(stats,sort_keys=True))
                if mode in ('original_edge30','rebound10','rebound20','rebound30','rebound20_strong') and cost=='base':
                    dest=Path('reports')/f'strategy_a_entry_{label}_{mode}_trades.csv'
                    with dest.open('w',newline='') as f:
                        if records:
                            w=csv.DictWriter(f,fieldnames=list(records[0]));w.writeheader();w.writerows(records)
        # Full baseline reproduction against independent original entry engine.
        orig,_=base.evaluate(h1,episodes,*base.COSTS['base'],True)
        baseline=out['variants']['original_edge30_base']
        assert (orig['final_usd'],orig['trades'],orig['max_h1_adverse_drawdown_pct']) == (
            baseline['final_usd'],baseline['trades'],baseline['drawdown_pct']),(orig,baseline)
        report['segments'][label]=out
    (Path('reports')/'strategy_a_entry_optimization_report.json').write_text(json.dumps(report,indent=2)+'\n')
    print('RESEARCH ONLY: independently reproduced baseline, no live trading.')

if __name__=='__main__':main()
