"""RESEARCH ONLY: Strategy B. Never sends orders or touches Strategy A.

Frozen prior 80 H1 channel from the existing Range/ATR<5 detector, daily 00 UTC,
12 H1 hours. A midpoint touch opens ONE equal-EUR long and short. At 90% channel
(long side) close long and add short, or at 10% close short and add long. From
then on close the TWO remaining same-side legs at midpoint (success) or the
corresponding OUTER channel edge (stop); at 12h flatten everything. After a
completed cycle, next entry is possible only in a LATER H1 bar. One initial
midpoint touch may support one cycle; never assume a fill unless price touches.

BID OHLC bars cannot determine multiple intrabar crossings. Do not act on
pivot in the midpoint-entry bar; if the outer edge is hit in this bar, unwind
the equal pair at the close and flag ambiguity. If BOTH pivots hit in the same
bar, unwind the equal pair at close, flag; no invented winning direction. If
pivot AND stop hit, adverse stop wins. If pivot AND midpoint hit, do not count
return until next hour. If stop AND midpoint hit for an already active pair,
stop wins. Gaps are filled at bar OPEN if more adverse than trigger. These are
research conventions, not actual tick chronology or executable quotes.

Costs: BID historical + hypothetical fixed spread and slip per fill; no real
ASK, commission, broker hedging permission, margin, financing, swap or guarantee.
The 2023-26 segment is ALREADY INSPECTED, not untouched out-of-sample.
"""
import csv
import datetime as dt
import hashlib
import json
import math
import sys
from pathlib import Path

from aggressive_range_backtest_h1 import INITIAL_USD, COSTS, LOT_UNITS
from compare_tpo_atr_h1 import HORIZON, collect, range_atr_signal
from range_guard_h1 import load_h1, execution
from trend_h4 import PIP, load_h4, indicators

RISK_FRACTION = .015
MAX_SINGLE_LEG_USD_NOTIONAL_TO_EQUITY = 3.0  # triple gross <= 9x, NOT broker leverage
MID = .50
PIVOT = .90


def levels(low, high):
    assert 0 < low < high
    w = high-low
    return low+w*.10, low+w*.50, low+w*.90


def size(balance, midpoint, width, spread, slip):
    # Loss of ideal complete stop cycle is 0.2 width per one unit; buffer costs.
    risk_unit = .20*width + (4*spread+8*slip)*PIP
    risk_units = math.floor(balance*RISK_FRACTION/risk_unit/LOT_UNITS)*LOT_UNITS
    nominal_units = math.floor(balance*MAX_SINGLE_LEG_USD_NOTIONAL_TO_EQUITY/midpoint/LOT_UNITS)*LOT_UNITS
    return int(max(0,min(risk_units,nominal_units)))


def cycle(bars, lower, upper, initial_balance, spread, slip, max_cycles=3):
    assert len(bars)==HORIZON and lower<upper
    low_pivot, mid, upper_pivot=levels(lower,upper)
    balance=initial_balance
    active=True
    p=None
    cycles=0
    counts={k:0 for k in ('midpoint_entries','pivot_upper','pivot_lower',
        'midpoint_returns','outer_stops','pivot_expiries','paired_expiries',
        'entry_hour_outer_ambiguous','entry_hour_pivot_deferred',
        'two_pivots_same_hour_ambiguous','pivot_and_stop_same_hour',
        'pivot_and_midpoint_same_hour','stop_and_midpoint_same_hour',
        'gap_adverse_stops','gap_pivot_beyond_edge','pause_outer',
        'pause_ratio','resume','skipped_size','cycles_positive',
        'cycles_negative','cycles_zero')}
    finished=[]
    trades=[]

    def log(t, event, side, bid, units, fill, pnl):
        trades.append({'utc':t.isoformat(),'event':event,'direction':side,
                       'reference_bid':round(bid,6),'fill':round(fill,6),
                       'units_eur':units,'realized_usd':round(pnl,6)})

    def fill(bid, side):
        return execution(bid,side,spread,slip)

    def realize(t,event,side,bid,entry,units):
        nonlocal balance
        price=fill(bid,-side)
        pnl=side*units*(price-entry)
        balance+=pnl
        p['result']+=pnl
        log(t,event,'long' if side==1 else 'short',bid,units,price,pnl)

    def end_cycle(t,reason):
        nonlocal p
        value=p['result']
        finished.append({'opened_utc':p['opened'].isoformat(),
                         'closed_utc':t.isoformat(),'reason':reason,
                         'direction':'up' if p['pivot']==1 else 'down' if p['pivot']==-1 else 'none',
                         'units_eur':p['units'],'gross_channel_width':round(upper-lower,6),
                         'pnl_usd':round(value,6)})
        if value>1e-7:counts['cycles_positive']+=1
        elif value< -1e-7:counts['cycles_negative']+=1
        else:counts['cycles_zero']+=1
        p=None

    def exit_legs(t,bid,reason):
        nonlocal p
        if p['state']=='paired':
            realize(t,reason+'_long',1,bid,p['long_entry'],p['units'])
            realize(t,reason+'_short',-1,bid,p['short_entry'],p['units'])
        elif p['state']=='short2':
            realize(t,reason+'_first_short',-1,bid,p['short_entry'],p['units'])
            realize(t,reason+'_new_short',-1,bid,p['second_entry'],p['units'])
        else:
            assert p['state']=='long2'
            realize(t,reason+'_first_long',1,bid,p['long_entry'],p['units'])
            realize(t,reason+'_new_long',1,bid,p['second_entry'],p['units'])
        end_cycle(t,reason)

    for j,(t,op,hi,lo,cl) in enumerate(bars):
        when=t+dt.timedelta(hours=1)
        # A later bar is required for a new cycle (no look-ahead same-hour reuse).
        entered=False
        if p is None and active and cycles<max_cycles and lo<=mid<=hi:
            units=size(balance,mid,upper-lower,spread,slip)
            if units>=LOT_UNITS:
                lp=fill(mid,1);sp=fill(mid,-1)
                p={'state':'paired','opened':t,'units':units,'long_entry':lp,
                   'short_entry':sp,'second_entry':None,'result':0.,'pivot':0}
                log(t,'enter_long','long',mid,units,lp,0.)
                log(t,'enter_short','short',mid,units,sp,0.)
                cycles+=1;counts['midpoint_entries']+=1
                entered=True
                if hi>=upper_pivot or lo<=low_pivot:
                    counts['entry_hour_pivot_deferred']+=1
                if hi>=upper or lo<=lower:
                    counts['entry_hour_outer_ambiguous']+=1
                    exit_legs(when,cl,'entry_bar_outer_ambiguous')
            else:
                counts['skipped_size']+=1

        if p is not None and not entered:
            if p['state']=='paired':
                up=hi>=upper_pivot
                down=lo<=low_pivot
                if up and down:
                    counts['two_pivots_same_hour_ambiguous']+=1
                    exit_legs(when,cl,'both_pivots_ambiguous')
                elif up or down:
                    side=1 if up else -1
                    pivot=upper_pivot if side==1 else low_pivot
                    edge=upper if side==1 else lower
                    if (op>=edge if side==1 else op<=edge):
                        # Already beyond stop at bar open: no fabricated pivot profit.
                        counts['gap_pivot_beyond_edge']+=1
                        exit_legs(t,op,'gap_past_outer_pair')
                    else:
                        at=(max(op,pivot) if side==1 else min(op,pivot))
                        # Matching sale of long and opening of second short or inverse.
                        if side==1:
                            realize(t,'close_long_at_upper_pivot',1,at,p['long_entry'],p['units'])
                            p['second_entry']=fill(at,-1)
                            log(t,'open_second_short','short',at,p['units'],p['second_entry'],0.)
                            p['state']='short2';counts['pivot_upper']+=1
                        else:
                            realize(t,'close_short_at_lower_pivot',-1,at,p['short_entry'],p['units'])
                            p['second_entry']=fill(at,1)
                            log(t,'open_second_long','long',at,p['units'],p['second_entry'],0.)
                            p['state']='long2';counts['pivot_lower']+=1
                        p['pivot']=side
                        if (hi>=edge if side==1 else lo<=edge):
                            counts['pivot_and_stop_same_hour']+=1
                            exit_bid=max(op,edge) if side==1 else min(op,edge)
                            exit_legs(when,exit_bid,'outer_stop')
                            counts['outer_stops']+=1
                        elif (lo<=mid if side==1 else hi>=mid):
                            counts['pivot_and_midpoint_same_hour']+=1
                            # No guaranteed return AFTER pivot in unknown H1 path.
            elif p['state']=='short2':
                stop=hi>=upper or op>=upper
                returned=lo<=mid or op<=mid
                if stop and returned:counts['stop_and_midpoint_same_hour']+=1
                if stop:
                    bid=max(op,upper)
                    if op>upper:counts['gap_adverse_stops']+=1
                    exit_legs(when,bid,'outer_stop');counts['outer_stops']+=1
                elif returned:
                    bid=min(op,mid)
                    exit_legs(when,bid,'midpoint_return');counts['midpoint_returns']+=1
            else:
                assert p['state']=='long2'
                stop=lo<=lower or op<=lower
                returned=hi>=mid or op>=mid
                if stop and returned:counts['stop_and_midpoint_same_hour']+=1
                if stop:
                    bid=min(op,lower)
                    if op<lower:counts['gap_adverse_stops']+=1
                    exit_legs(when,bid,'outer_stop');counts['outer_stops']+=1
                elif returned:
                    bid=max(op,mid)
                    exit_legs(when,bid,'midpoint_return');counts['midpoint_returns']+=1

        # Existing positions stay managed, but no new midpoint entries while paused.
        if hi>upper or lo<lower:
            if active:counts['pause_outer']+=1
            active=False
        else:
            # Only at H1 CLOSE, affecting next bar.
            past=bars[:j+1]
            # Caller supplies precomputed real ratios below; this per-cycle
            # field will be overwritten by run_segment's ratios argument.
            current_ratio=bars.ratios[j] if hasattr(bars,'ratios') else None
            if current_ratio is None or current_ratio>=5:
                if active:counts['pause_ratio']+=1
                active=False
            elif not active and j<HORIZON-1:
                active=True;counts['resume']+=1

    if p is not None:
        t=bars[-1][0]+dt.timedelta(hours=1)
        if p['state']=='paired':
            exit_legs(t,bars[-1][4],'paired_expiry');counts['paired_expiries']+=1
        else:
            exit_legs(t,bars[-1][4],'pivot_expiry');counts['pivot_expiries']+=1
    assert p is None and cycles<=max_cycles
    assert abs(initial_balance+sum(x['pnl_usd'] for x in finished)-balance)<.01
    return balance,counts,finished,trades


class Window(list):
    """H1 bars plus completed-at-close ratios, no additional future information."""
    def __init__(self,bars,ratios):
        super().__init__(bars)
        self.ratios=ratios


def run_segment(h1,episodes,spread,slip):
    index={x[0]:i for i,x in enumerate(h1)}
    balance=INITIAL_USD
    all_positions=[];all_legs=[];totals={};participating=0
    for e in episodes:
        if not e['range_atr']:continue
        i=index[e['time']]
        ratios=[range_atr_signal(h1[i-80:i+j+1]) for j in range(HORIZON)]
        window=Window(h1[i:i+HORIZON],ratios)
        balance,counters,positions,legs=cycle(window,e['lower'],e['upper'],balance,spread,slip)
        participating+=bool(positions)
        all_positions.extend({'episode_utc':e['time'].isoformat(),**v} for v in positions)
        all_legs.extend({'episode_utc':e['time'].isoformat(),**v} for v in legs)
        for k,v in counters.items():totals[k]=totals.get(k,0)+v
    winners=sum(p['pnl_usd']>0 for p in all_positions)
    losers=sum(p['pnl_usd']<0 for p in all_positions)
    total_pivot=totals['pivot_upper']+totals['pivot_lower']
    terminal=totals['midpoint_returns']+totals['outer_stops']+totals['pivot_expiries']
    assert total_pivot==terminal
    breakdown={}
    for reason in sorted({x['reason'] for x in all_positions}):
        records=[x for x in all_positions if x['reason']==reason]
        breakdown[reason]={'count':len(records),'net_pnl_usd':round(sum(x['pnl_usd'] for x in records),2),
                           'positive':sum(x['pnl_usd']>0 for x in records)}
    return {'initial_usd':INITIAL_USD,'final_usd':round(balance,2),
        'return_pct':round(100*(balance/INITIAL_USD-1),2),
        'signals':sum(x['range_atr'] for x in episodes),
        'episodes_with_entry':participating,'completed_cycles':len(all_positions),
        'positive_cycles':winners,'negative_cycles':losers,
        'triggered_pivots':total_pivot,'resolved_pivot_outcomes':terminal,
        'conditional_return_before_edge_pct':round(100*totals['midpoint_returns']/terminal,2) if terminal else None,
        'conditional_return_excluding_expiry_pct':round(100*totals['midpoint_returns']/(totals['midpoint_returns']+totals['outer_stops']),2) if totals['midpoint_returns']+totals['outer_stops'] else None,
        'events':totals,'exit_breakdown':breakdown},all_positions,all_legs


def self_test():
    a,b,c=levels(1,2)
    assert abs(a-1.1)<1e-10 and b==1.5 and abs(c-1.9)<1e-10
    assert size(10000,1.5,1,0,0)>0
    def test_prices(xs,expected):
        t=dt.datetime(2026,1,1)
        bars=Window([(t+dt.timedelta(hours=j),*row) for j,row in enumerate(xs)], [2.]*12)
        result,c,finished,_=cycle(bars,1,2,10000,0,0,max_cycles=1)
        assert c[expected]==1,(expected,c)
        return result,finished
    # 12 H1 bars, first midpoint touched, second pivot, third terminal.
    pre=[(1.5,1.51,1.49,1.5),(1.5,1.91,1.49,1.9)]
    win=pre+[(1.9,1.91,1.49,1.5)]+[(1.5,1.6,1.4,1.5)]*9
    loss=pre+[(1.9,2.01,1.80,2.0)]+[(2.,2.,2.,2.)]*9
    p,cycles=test_prices(win,'midpoint_returns');q,other=test_prices(loss,'outer_stops')
    assert p>10000 and q<10000 and len(cycles)==len(other)==1
    # Equal midpoint open legs should have zero mark-to-market price delta,
    # and gross winner=0.8 * units; loser=-0.2 * units with zero costs.
    assert abs((p-10000)/cycles[0]['units_eur']-.8)<1e-8
    assert abs((q-10000)/other[0]['units_eur']+.2)<1e-8
    print('Strategy B midpoint success +0.8W and outer stop -0.2W synthetic tests PASS')


def main():
    if len(sys.argv)==2 and sys.argv[1]=='--self-test':self_test();return
    if len(sys.argv)!=2:raise SystemExit('Usage: strategy_b_midpoint_reversal_h1.py data/eurusd_h1.csv')
    path=Path(sys.argv[1]);h1=load_h1(path);h4=load_h4(path)
    _,_,atr,_=indicators(h4)
    split=int(len(h4)*.7)
    end=h4[-1][0]+dt.timedelta(hours=4)
    report={'data_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'strategy':'B midpoint equal long+short; pivot 10/90pct, midpoint target, edge stop',
        'params':{'midpoint_fraction':MID,'pivot_fraction':PIVOT,
                  'units':'equal units EUR for all legs','max_cycles_per_12h':3,
                  'risk_fraction':RISK_FRACTION,'max_single_leg_notional_x':MAX_SINGLE_LEG_USD_NOTIONAL_TO_EQUITY,
                  'costs_pips':COSTS,'outside_wick_pause_new_cycles':True},
        'limitations':'BID-only H1 intrabar order ambiguous; pivot-and-stop stop-first; pivot-and-midpoint deferred; midpoint-entry hour pivot deferred; both pivots ambiguous forced flat. Slippage/spread hypothetical; no margin, commission, swap, live hedging validation. Already inspected 2023-26, no clean OOS.',
        'segments':{}}
    Path('reports').mkdir(exist_ok=True)
    for name,start,finish in (('development',h4[200][0],h4[split][0]),
                              ('previously_inspected_2023_2026',h4[split][0],end)):
        episodes,audit=collect(h1,h4,atr,start,finish)
        segment={'audit':audit,'variants':{}}
        for cost,(spread,slip) in COSTS.items():
            metrics,positions,legs=run_segment(h1,episodes,spread,slip)
            segment['variants'][cost]=metrics
            print(name,cost,json.dumps(metrics,sort_keys=True))
            for kind,records in (('cycles',positions),('legs',legs)):
                with (Path('reports')/f'strategy_b_{name}_{cost}_{kind}.csv').open('w',newline='') as f:
                    if records:
                        w=csv.DictWriter(f,fieldnames=list(records[0]));w.writeheader();w.writerows(records)
        report['segments'][name]=segment
    (Path('reports')/'strategy_b_midpoint_report.json').write_text(json.dumps(report,indent=2)+'\n')
    print('RESEARCH ONLY: no broker orders; no verified tick chronology.')

if __name__=='__main__':main()
