"""Research-only Strategy B. No orders, broker connections, or Strategy A edits.

Frozen prior 80 H1 extremes, Range/ATR <5, one signal at 00 UTC, 12-hour
windows. Enter equal-unit EUR long+short only when midpoint actually touched.
At 90% close long, open second short; at 10% close short, open second long.
Close two remaining same-direction legs at midpoint (return) or respective
outer boundary (stop); expiry closes every leg. Up to three cycles per window,
never a second entry in the same H1 as previous completion.

BID-only H1 ambiguous chronology: entry-hour pivot deferred, entry-hour
outer breach flattens paired legs; both pivots in same later H1 flatten pair;
pivot + adverse outer boundary -> outer stop first, pivot + midpoint -> return
not permitted until later bar; existing stop + return -> outer stop first.
The conservatively modelled event ordering is NOT tick-realizable evidence.
Historical 2023-26 is already inspected, NOT independent OOS. Hypothetical
fixed spread/slippage only; no commission, margin, swap, genuine ask, broker
hedge authorization, assured fills, or actual trading.
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

RISK_FRACTION=.015
MAX_SINGLE_LEG_USD_NOTIONAL_TO_EQUITY=3.0 # maximum triple gross ~9x; no real margin model


def levels(lower,upper):
    assert 0<lower<upper
    width=upper-lower
    return lower+.1*width,lower+.5*width,lower+.9*width


def size(balance,mid,width,spread,slip):
    risk=.2*width+(4*spread+8*slip)*PIP
    by_risk=math.floor(balance*RISK_FRACTION/risk/LOT_UNITS)*LOT_UNITS
    by_nominal=math.floor(balance*MAX_SINGLE_LEG_USD_NOTIONAL_TO_EQUITY/mid/LOT_UNITS)*LOT_UNITS
    return int(max(0,min(by_risk,by_nominal)))


class Window(list):
    """Ratios calculated only using bars closed by respective H1 close."""
    def __init__(self,bars,ratios):
        super().__init__(bars)
        self.ratios=ratios


def cycle(window,lower,upper,initial_balance,spread,slip,max_cycles=3):
    assert len(window)==HORIZON and lower<upper and len(window.ratios)==HORIZON
    low_pivot,mid,high_pivot=levels(lower,upper)
    balance=initial_balance
    armed=True
    pos=None
    started=0
    events={key:0 for key in (
        'midpoint_entries','pivot_upper','pivot_lower','midpoint_returns',
        'outer_stops','pivot_expiries','paired_expiries',
        'entry_hour_outer_ambiguous','entry_hour_pivot_deferred',
        'two_pivots_same_hour_ambiguous','pivot_and_stop_same_hour',
        'pivot_and_midpoint_same_hour','stop_and_midpoint_same_hour',
        'gap_adverse_stops','gap_pivot_beyond_edge','pause_outer',
        'pause_ratio','resume','skipped_size','cycles_positive',
        'cycles_negative','cycles_zero')}
    completed=[]
    ledger=[]

    def quote(bid,side):return execution(bid,side,spread,slip)

    def log(t,event,side,bid,units,price,pnl=0):
        ledger.append({'utc':t.isoformat(),'event':event,'direction':side,
                       'reference_bid':round(bid,6),'fill':round(price,6),
                       'units_eur':units,'realized_usd':round(pnl,6)})

    def realize(t,event,side,bid,entry,units):
        nonlocal balance
        price=quote(bid,-side)
        pnl=side*units*(price-entry)
        balance+=pnl
        pos['pnl']+=pnl
        log(t,event,'long' if side==1 else 'short',bid,units,price,pnl)

    def finalize(t,reason):
        nonlocal pos
        completed.append({'opened_utc':pos['opened'].isoformat(),
            'closed_utc':t.isoformat(),'reason':reason,
            'direction':{0:'none',1:'up',-1:'down'}[pos['pivot']],
            'units_eur':pos['units'],'gross_channel_width':round(upper-lower,6),
            'pnl_usd':round(pos['pnl'],6)})
        if pos['pnl']>1e-7:events['cycles_positive']+=1
        elif pos['pnl']< -1e-7:events['cycles_negative']+=1
        else:events['cycles_zero']+=1
        pos=None

    def close(t,bid,reason):
        if pos['state']=='paired':
            realize(t,reason+'_long',1,bid,pos['long_entry'],pos['units'])
            realize(t,reason+'_short',-1,bid,pos['short_entry'],pos['units'])
        elif pos['state']=='short2':
            realize(t,reason+'_first_short',-1,bid,pos['short_entry'],pos['units'])
            realize(t,reason+'_new_short',-1,bid,pos['second_entry'],pos['units'])
        else:
            assert pos['state']=='long2'
            realize(t,reason+'_first_long',1,bid,pos['long_entry'],pos['units'])
            realize(t,reason+'_new_long',1,bid,pos['second_entry'],pos['units'])
        finalize(t,reason)

    for j,(t,op,hi,lo,cl) in enumerate(window):
        end=t+dt.timedelta(hours=1)
        entered=False
        if pos is None and armed and started<max_cycles and lo<=mid<=hi:
            units=size(balance,mid,upper-lower,spread,slip)
            if units>=LOT_UNITS:
                lp=quote(mid,1);sp=quote(mid,-1)
                pos={'state':'paired','opened':t,'units':units,
                     'long_entry':lp,'short_entry':sp,
                     'second_entry':None,'pnl':0.,'pivot':0}
                log(t,'enter_long','long',mid,units,lp)
                log(t,'enter_short','short',mid,units,sp)
                entered=True;started+=1;events['midpoint_entries']+=1
                if hi>=high_pivot or lo<=low_pivot:
                    events['entry_hour_pivot_deferred']+=1
                if hi>=upper or lo<=lower:
                    events['entry_hour_outer_ambiguous']+=1
                    close(end,cl,'entry_bar_outer_ambiguous')
            else:events['skipped_size']+=1

        if pos is not None and not entered:
            if pos['state']=='paired':
                up=hi>=high_pivot
                down=lo<=low_pivot
                if up and down:
                    events['two_pivots_same_hour_ambiguous']+=1
                    close(end,cl,'both_pivots_ambiguous')
                elif up or down:
                    direction=1 if up else -1
                    pivot=high_pivot if up else low_pivot
                    edge=upper if up else lower
                    if (op>=edge if up else op<=edge):
                        events['gap_pivot_beyond_edge']+=1
                        close(t,op,'gap_past_outer_pair')
                    else:
                        at=max(op,pivot) if up else min(op,pivot)
                        if up:
                            realize(t,'close_long_upper_pivot',1,at,pos['long_entry'],pos['units'])
                            pos['second_entry']=quote(at,-1)
                            log(t,'open_second_short','short',at,pos['units'],pos['second_entry'])
                            pos['state']='short2';events['pivot_upper']+=1
                        else:
                            realize(t,'close_short_lower_pivot',-1,at,pos['short_entry'],pos['units'])
                            pos['second_entry']=quote(at,1)
                            log(t,'open_second_long','long',at,pos['units'],pos['second_entry'])
                            pos['state']='long2';events['pivot_lower']+=1
                        pos['pivot']=direction
                        if (hi>=edge if up else lo<=edge):
                            events['pivot_and_stop_same_hour']+=1
                            stop_bid=max(op,edge) if up else min(op,edge)
                            close(end,stop_bid,'outer_stop');events['outer_stops']+=1
                        elif (lo<=mid if up else hi>=mid):
                            # Order of target/pivot unknown; do not invent same-H1 win.
                            events['pivot_and_midpoint_same_hour']+=1
            elif pos['state']=='short2':
                stop=hi>=upper or op>=upper
                returned=lo<=mid or op<=mid
                if stop and returned:events['stop_and_midpoint_same_hour']+=1
                if stop:
                    at=max(op,upper)
                    if op>upper:events['gap_adverse_stops']+=1
                    close(end,at,'outer_stop');events['outer_stops']+=1
                elif returned:
                    close(end,min(op,mid),'midpoint_return')
                    events['midpoint_returns']+=1
            else:
                assert pos['state']=='long2'
                stop=lo<=lower or op<=lower
                returned=hi>=mid or op>=mid
                if stop and returned:events['stop_and_midpoint_same_hour']+=1
                if stop:
                    at=min(op,lower)
                    if op<lower:events['gap_adverse_stops']+=1
                    close(end,at,'outer_stop');events['outer_stops']+=1
                elif returned:
                    close(end,max(op,mid),'midpoint_return')
                    events['midpoint_returns']+=1

        if hi>upper or lo<lower:
            if armed:events['pause_outer']+=1
            armed=False
        else:
            ratio=window.ratios[j]
            if ratio is None or ratio>=5:
                if armed:events['pause_ratio']+=1
                armed=False
            elif not armed and j<HORIZON-1:
                armed=True;events['resume']+=1

    if pos is not None:
        end=window[-1][0]+dt.timedelta(hours=1)
        if pos['state']=='paired':
            close(end,window[-1][4],'paired_expiry');events['paired_expiries']+=1
        else:
            close(end,window[-1][4],'pivot_expiry');events['pivot_expiries']+=1
    assert pos is None and started<=max_cycles
    assert abs(initial_balance+sum(x['pnl_usd'] for x in completed)-balance)<.01
    return balance,events,completed,ledger


def run_segment(h1,episodes,spread,slip):
    ix={bar[0]:i for i,bar in enumerate(h1)}
    balance=INITIAL_USD
    positions=[];orders=[];counts={};episodes_traded=0
    for e in episodes:
        if not e['range_atr']:continue
        i=ix[e['time']]
        w=Window(h1[i:i+HORIZON],
                 [range_atr_signal(h1[i-80:i+j+1]) for j in range(HORIZON)])
        balance,event,completed,legs=cycle(w,e['lower'],e['upper'],balance,spread,slip)
        episodes_traded+=bool(completed)
        positions.extend({'episode_utc':e['time'].isoformat(),**x} for x in completed)
        orders.extend({'episode_utc':e['time'].isoformat(),**x} for x in legs)
        for name,n in event.items():counts[name]=counts.get(name,0)+n
    outcomes=counts['midpoint_returns']+counts['outer_stops']+counts['pivot_expiries']
    pivots=counts['pivot_upper']+counts['pivot_lower']
    assert outcomes==pivots
    breakdown={}
    for reason in sorted({x['reason'] for x in positions}):
        xs=[x for x in positions if x['reason']==reason]
        breakdown[reason]={'count':len(xs),
            'net_pnl_usd':round(sum(x['pnl_usd'] for x in xs),2),
            'positive':sum(x['pnl_usd']>0 for x in xs)}
    denom=counts['midpoint_returns']+counts['outer_stops']
    return {'initial_usd':INITIAL_USD,'final_usd':round(balance,2),
        'return_pct':round(100*(balance/INITIAL_USD-1),2),
        'signals':sum(e['range_atr'] for e in episodes),
        'episodes_with_entry':episodes_traded,'completed_cycles':len(positions),
        'positive_cycles':sum(x['pnl_usd']>0 for x in positions),
        'negative_cycles':sum(x['pnl_usd']<0 for x in positions),
        'triggered_pivots':pivots,'resolved_pivot_outcomes':outcomes,
        'return_before_edge_including_expiry_pct':round(100*counts['midpoint_returns']/outcomes,2) if outcomes else None,
        'return_before_edge_resolved_only_pct':round(100*counts['midpoint_returns']/denom,2) if denom else None,
        'events':counts,'exit_breakdown':breakdown},positions,orders


def self_test():
    a,b,c=levels(1,2)
    assert abs(a-1.1)<1e-10 and b==1.5 and abs(c-1.9)<1e-10
    # The pedagogical 1--2 channel has unrealistic EURUSD width. Use enough
    # hypothetical capital to meet the real min 1000-EUR lot increment.
    initial=1000000.
    assert size(initial,1.5,1.,0.,0.)>=LOT_UNITS
    t=dt.datetime(2026,1,1)
    pre=[(1.5,1.51,1.49,1.5),(1.5,1.91,1.49,1.9)]
    win=pre+[(1.9,1.91,1.49,1.5)]+[(1.5,1.6,1.4,1.5)]*9
    lose=pre+[(1.9,2.01,1.80,2.0)]+[(2.,2.,2.,2.)]*9
    def execute(rows,key):
        w=Window([(t+dt.timedelta(hours=j),*row) for j,row in enumerate(rows)],
                 [2.]*HORIZON)
        final,events,done,_=cycle(w,1.,2.,initial,0.,0.,max_cycles=1)
        assert events[key]==1,(key,events)
        assert len(done)==1
        return final,done[0]
    winning,wp=execute(win,'midpoint_returns')
    losing,lp=execute(lose,'outer_stops')
    assert abs((winning-initial)/wp['units_eur']-.8)<1e-8
    assert abs((losing-initial)/lp['units_eur']+.2)<1e-8
    print('Strategy B synthetic successful return +0.8W and outer stop -0.2W: PASS')


def main():
    if len(sys.argv)==2 and sys.argv[1]=='--self-test':self_test();return
    if len(sys.argv)!=2:raise SystemExit('Usage: python src/strategy_b_midpoint_reversal_h1.py data/eurusd_h1.csv')
    path=Path(sys.argv[1]);h1=load_h1(path);h4=load_h4(path)
    _,_,atr,_=indicators(h4)
    split=int(len(h4)*.7)
    end=h4[-1][0]+dt.timedelta(hours=4)
    out={'data_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'strategy':'B equal-unit initial long+short at midpoint, 10/90 pivot, return midpoint or stop outer edge',
        'parameters':{'risk_fraction':RISK_FRACTION,'max_single_leg_notional_x':MAX_SINGLE_LEG_USD_NOTIONAL_TO_EQUITY,
                      'max_cycles_12h':3,'cost_spread_slippage_pips':COSTS,
                      'entry':'midpoint must be touched; entry-bar pivots deferred',
                      'existing_flat_detector':'prior 20 H1 range/ATR14 < 5; prior 80 H1 frozen channel'},
        'limitations':'BID-only H1, ambiguous event order, prior observed 2023-26, no genuine ask, broker hedge account/margin, commissions or swap. 00UTC daily sampled, not continuous intraday. Hypothetical, no orders.',
        'segments':{}}
    Path('reports').mkdir(exist_ok=True)
    for name,start,finish in (('development',h4[200][0],h4[split][0]),
                              ('previously_inspected_2023_2026',h4[split][0],end)):
        episodes,audit=collect(h1,h4,atr,start,finish)
        segment={'audit':audit,'variants':{}}
        for cost,(spread,slip) in COSTS.items():
            metrics,cycles,legs=run_segment(h1,episodes,spread,slip)
            segment['variants'][cost]=metrics
            print(name,cost,json.dumps(metrics,sort_keys=True))
            for kind,rows in (('cycles',cycles),('legs',legs)):
                with (Path('reports')/f'strategy_b_{name}_{cost}_{kind}.csv').open('w',newline='') as f:
                    if rows:
                        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
        out['segments'][name]=segment
    (Path('reports')/'strategy_b_midpoint_report.json').write_text(json.dumps(out,indent=2)+'\n')
    print('RESEARCH ONLY. No broker orders, not tick-verified.')

if __name__=='__main__':main()
