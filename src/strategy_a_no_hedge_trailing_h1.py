"""RESEARCH ONLY: Strategy A, no hedging, no broker connection or orders.

Identical 00 UTC Range/ATR<5 opportunity and frozen prior 80 H1 channel as
previous Strategy A, 12h window, entry at known H1 open in outer 30%, max 3
entries per window, only one open position at a time. Protective stop at outer
boundary. Profit activation requires a COMPLETED H1 close at/through 52% of
channel; from NEXT bar the protective stop is 52% or close minus/plus 2% of
channel width, ratcheted using completed closes only. At 12h exit remaining.
This avoids lookahead but does not capture same-hour touch-and-reverse.
Bid-only data, hypothetical spread/slippage, not real ASK/tick fills, margin,
commissions or swaps. Prior 2023-2026 segment inspected repeatedly (not OOS).
"""
import csv
import datetime as dt
import hashlib
import json
import math
import sys
from pathlib import Path
from aggressive_range_backtest_h1 import INITIAL_USD, COSTS, ENTRY_FRACTION, MAX_TRADES_PER_CYCLE, LOT_UNITS
from compare_tpo_atr_h1 import HORIZON, collect, range_atr_signal, simple_atr
from range_guard_h1 import execution, load_h1
from trend_h4 import PIP, indicators, load_h4

ACTIVATION = .52
TRAIL_GAP = .02
RISK = .015
NOTIONAL_CAP = 4.0


def target_bid(side, lower, upper):
    width = upper - lower
    return lower + (ACTIVATION if side == 1 else 1-ACTIVATION)*width


def stop_bid(side, lower, upper, spread):
    return lower if side == 1 else upper - spread*PIP


def simulate_cycle(bars, historic, lower, upper, balance, peak, drawdown, spread, slip,
                   trailing=True):
    if len(bars) != HORIZON or len(historic) != HORIZON or lower >= upper:
        raise ValueError('Invalid cycle inputs')
    width = upper-lower
    start_balance = balance
    armed = True
    pos = None
    opened = 0
    events = {k:0 for k in ('entries','stops','trailing_exits','fixed52_exits',
        'expiries','trailing_armed','skipped_signal','skipped_size','session_block',
        'boundary_pauses','atr_pauses','resumes','stop_gaps','trailing_gaps',
        'stop_and_activation_same_h1','trailing_and_boundary_same_h1')}
    trades=[]
    fills=[]

    def record(when, event, side, bid, fill, units, pnl=0.):
        fills.append({'timestamp':when.isoformat(),'event':event,
            'side':'long' if side == 1 else 'short','bid':round(bid,7),
            'executed_price':round(fill,7),'units_eur':units,'pnl_usd':round(pnl,5)})

    def mark(bid):
        nonlocal drawdown
        if pos is not None:
            value = balance + pos['side']*pos['units']*(execution(bid,-pos['side'],spread,slip)-pos['entry'])
        else:
            value=balance
        if peak > 0:
            drawdown=max(drawdown,(peak-value)/peak)

    def exit_main(bid, reason, when):
        nonlocal pos,balance,peak
        p=pos
        fill=execution(bid,-p['side'],spread,slip)
        pnl=p['side']*p['units']*(fill-p['entry'])
        balance+=pnl
        record(when,reason,-p['side'],bid,fill,p['units'],pnl)
        trades.append({'opened_utc':p['opened'].isoformat(),'closed_utc':when.isoformat(),
            'side':'long' if p['side']==1 else 'short','entry_price':round(p['entry'],7),
            'exit_price':round(fill,7),'units_eur':p['units'],
            'pnl_usd':round(pnl,5),'reason':reason,'trailing_was_armed':p['armed']})
        events[reason]+=1
        pos=None
        peak=max(peak,balance)
        mark(bid)

    for j,(when,op,hi,lo,cl) in enumerate(bars):
        end=when+dt.timedelta(hours=1)
        if pos is None and armed and lower <= op <= upper and opened < MAX_TRADES_PER_CYCLE:
            if balance-start_balance <= -.03*start_balance:
                events['session_block']+=1
            else:
                side=1 if op <= lower+ENTRY_FRACTION*width else -1 if op >= upper-ENTRY_FRACTION*width else 0
                if side:
                    entry=execution(op,side,spread,slip)
                    boundary=stop_bid(side,lower,upper,spread)
                    stopped=execution(boundary,-side,spread,slip)
                    risk_per_unit=side*(entry-stopped)
                    activation=target_bid(side,lower,upper)
                    potential=side*(execution(activation,-side,spread,slip)-entry)
                    atr=historic[j][1]
                    if atr is None or risk_per_unit <= 0 or risk_per_unit < atr or potential <= 0:
                        events['skipped_signal']+=1
                    else:
                        by_risk=math.floor(balance*RISK/risk_per_unit/LOT_UNITS)*LOT_UNITS
                        by_cap=math.floor(balance*NOTIONAL_CAP/entry/LOT_UNITS)*LOT_UNITS
                        units=int(min(by_risk,by_cap))
                        if units < LOT_UNITS:
                            events['skipped_size']+=1
                        else:
                            pos={'opened':when,'side':side,'units':units,'entry':entry,
                                 'armed':False,'trail':None}
                            opened+=1
                            events['entries']+=1
                            record(when,'open',side,op,entry,units)
                            mark(op)

        if pos is not None:
            side=pos['side']
            outer=stop_bid(side,lower,upper,spread)
            activation=target_bid(side,lower,upper)
            hit_outer=lo<=outer if side==1 else hi>=outer
            hit_activation=hi>=activation if side==1 else lo<=activation
            if hit_outer and hit_activation and not pos['armed']:
                events['stop_and_activation_same_h1']+=1
            if pos['armed']:
                trail=pos['trail']
                hit_trail=lo<=trail if side==1 else hi>=trail
                if hit_trail and hit_outer:
                    events['trailing_and_boundary_same_h1']+=1
                if hit_trail:
                    price=min(op,trail) if side==1 else max(op,trail)
                    if (op<trail if side==1 else op>trail):
                        events['trailing_gaps']+=1
                    mark(price)
                    exit_main(price,'trailing_exits',end)
                elif hit_outer:
                    # Should be unreachable for a trailing floor inside the channel.
                    price=min(op,outer) if side==1 else max(op,outer)
                    mark(price)
                    exit_main(price,'stops',end)
            elif hit_outer:
                # If target and stop were both touched during one H1, adverse
                # boundary is processed first; no favorable intrabar ordering.
                price=min(op,outer) if side==1 else max(op,outer)
                if (op<outer if side==1 else op>outer):events['stop_gaps']+=1
                mark(price)
                exit_main(price,'stops',end)
            elif not trailing and hit_activation:
                mark(lo if side==1 else hi)
                exit_main(activation,'fixed52_exits',end)
            else:
                mark(lo if side==1 else hi)
                if trailing and (cl>=activation if side==1 else cl<=activation):
                    if not pos['armed']:
                        pos['armed']=True
                        events['trailing_armed']+=1
                    proposed=(max(activation,cl-TRAIL_GAP*width) if side==1 else
                              min(activation,cl+TRAIL_GAP*width))
                    if pos['trail'] is None:
                        pos['trail']=proposed
                    else:
                        pos['trail']=(max(pos['trail'],proposed) if side==1 else
                                      min(pos['trail'],proposed))
                elif pos is not None and pos['armed']:
                    # Preserve the prior trailing stop even if the close retreats.
                    pass
            if pos is not None:
                mark(cl)

        outside=lo<lower or hi>upper
        if outside:
            if armed:events['boundary_pauses']+=1
            armed=False
        elif armed:
            if historic[j][0] is None or historic[j][0]>=5:
                events['atr_pauses']+=1
                armed=False
        elif historic[j][0] is not None and historic[j][0]<5 and j<HORIZON-1:
            armed=True
            events['resumes']+=1

    if pos is not None:
        exit_main(bars[-1][4],'expiries',bars[-1][0]+dt.timedelta(hours=1))
    assert pos is None and opened<=MAX_TRADES_PER_CYCLE
    return balance,peak,drawdown,events,trades,fills


def evaluate(h1,episodes,spread,slip,trailing):
    lookup={row[0]:i for i,row in enumerate(h1)}
    balance=peak=INITIAL_USD
    dd=0.
    totals={}
    trades=[]
    fills=[]
    cycles=0
    for ep in episodes:
        if not ep['range_atr']:continue
        cycles+=1
        i=lookup[ep['time']]
        historic=[(range_atr_signal(h1[i-80:i+j+1]),simple_atr(h1[i-80:i+j]))
                  for j in range(HORIZON)]
        balance,peak,dd,ev,t,f=simulate_cycle(h1[i:i+HORIZON],historic,
            ep['lower'],ep['upper'],balance,peak,dd,spread,slip,trailing)
        for k,v in ev.items():totals[k]=totals.get(k,0)+v
        trades.extend({'cycle_utc':ep['time'].isoformat(),**x} for x in t)
        fills.extend({'cycle_utc':ep['time'].isoformat(),**x} for x in f)
    breakdown={}
    for reason in ('stops','trailing_exits','fixed52_exits','expiries'):
        rows=[row for row in trades if row['reason']==reason]
        breakdown[reason]={'count':len(rows),'net_usd':round(sum(row['pnl_usd'] for row in rows),2),
             'positive':sum(row['pnl_usd']>0 for row in rows),
             'negative':sum(row['pnl_usd']<0 for row in rows)}
    wins=sum(row['pnl_usd']>0 for row in trades)
    return {'initial_usd':INITIAL_USD,'final_usd':round(balance,2),
        'return_pct':round((balance/INITIAL_USD-1)*100,2),
        'max_h1_adverse_drawdown_pct':round(dd*100,2),
        'flat_cycles':cycles,'trades':len(trades),'wins':wins,
        'losses':sum(row['pnl_usd']<0 for row in trades),
        'events':totals,'breakdown':breakdown},trades,fills


def self_test():
    start=dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)
    def mk(rows):
        return [(start+dt.timedelta(hours=i),*row) for i,row in enumerate(rows)]
    atr=[(4.,.01)]*HORIZON
    happy=[(1.1,1.2,1.08,1.15),(1.15,1.55,1.1,1.54),
           (1.54,1.59,1.51,1.53)]+[(1.53,1.55,1.49,1.53)]*9
    _,_,_,e,t,f=simulate_cycle(mk(happy),atr,1.,2.,10000.,10000.,0.,1.5,.3,True)
    assert e['trailing_armed']==1 and e['trailing_exits']==1 and e['stops']==0,(e,t)
    assert t[0]['pnl_usd']>0 and len(t)==1 and not any('hedge' in x['event'] for x in f)
    adverse=[(1.1,1.6,.99,1.1)]+[(1.1,1.2,1.08,1.1)]*11
    _,_,_,e,t,f=simulate_cycle(mk(adverse),atr,1.,2.,10000.,10000.,0.,1.5,.3,True)
    assert e['stops']==1 and e['trailing_armed']==0 and e['stop_and_activation_same_h1']==1,(e,t)
    short=[(1.9,1.94,1.85,1.9),(1.9,1.91,1.45,1.46),
           (1.46,1.49,1.43,1.47)]+[(1.47,1.49,1.41,1.47)]*9
    _,_,_,e,t,f=simulate_cycle(mk(short),atr,1.,2.,10000.,10000.,0.,1.5,.3,True)
    assert e['trailing_armed']==1 and e['trailing_exits']==1 and t[0]['side']=='short',(e,t)
    print('Strategy A no hedge: long/short protected trail, adverse-first and no-hedge tests PASS')


def main():
    if len(sys.argv)==2 and sys.argv[1]=='--self-test':
        self_test();return
    if len(sys.argv)!=2:raise SystemExit('Usage: script data/eurusd_h1.csv')
    path=Path(sys.argv[1]);h1=load_h1(path);h4=load_h4(path)
    _,_,h4atr,_=indicators(h4)
    split=int(len(h4)*.7)
    spans=[('development',h4[200][0],h4[split][0]),
           ('previously_inspected_2023_2026',h4[split][0],h4[-1][0]+dt.timedelta(hours=4))]
    report={'data_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'strategy':'A without hedge: 30pct edge entries, boundary stop, 52pct close-confirmed activation and protected 2pct channel trailing, 12h expiry',
        'sizing':'1.5pct boundary-reference risk; max main position notional 4x cash equity; <=3 entries per window; realized loss gate 3pct',
        'costs':COSTS,
        'limitations':'H1 BID only, assumed fixed spread/slippage and no commission or swaps. 52pct arming and trailing updates ONLY on closed H1; next candle can gap through floor. Adverse stop first if same-bar activation. 2023-26 used repeatedly, not untouched OOS. Research only, no broker/margin model or orders.',
        'segments':{}}
    Path('reports').mkdir(exist_ok=True)
    for name,beg,end in spans:
        episodes,audit=collect(h1,h4,h4atr,beg,end)
        seg={'audit':audit,'variants':{}}
        for trailing in (False,True):
            for name_cost,(spread,slip) in COSTS.items():
                key=('trailing52_2' if trailing else 'fixed52')+'_'+name_cost
                metrics,trades,fills=evaluate(h1,episodes,spread,slip,trailing)
                seg['variants'][key]=metrics
                print(name,key,json.dumps(metrics,sort_keys=True))
                if name_cost=='base':
                    for suffix,rows in (('trades',trades),('fills',fills)):
                        dest=Path('reports')/f'strategy_a_nohedge_{name}_{key}_{suffix}.csv'
                        with dest.open('w',newline='') as f:
                            if rows:
                                writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
        report['segments'][name]=seg
    (Path('reports')/'strategy_a_nohedge_report.json').write_text(json.dumps(report,indent=2)+'\n')
    print('RESEARCH ONLY: no live order execution; no hedges.')

if __name__=='__main__':main()
