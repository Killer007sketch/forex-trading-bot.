"""RESEARCH ONLY. USD NOTIONAL (not EUR units or margin): hedge USD value at
hedge fill = 1.3 * main USD value at original main fill. Main/hedge quantities
are therefore DIFFERENT even before multiplying by 1.3. No live orders.

00 UTC daily signal, frozen prior 80 H1 extrema, 12 H1 duration. Enter main
at known H1 open inside outer 30%, target at 50/60/70% of channel. Hedge at
outer boundary before any same-candle target; KEEP original main. Equal or
1.3 USD-notional opposite hedge. Release hedge only on an upward crossing of
lower-offset (for main long), downward upper+offset (main short), and only
if the preceding COMPLETED H1 close was beyond the release level. This
excludes unverifiable immediate intrabar turnarounds. No same-H1 target or
rehedge after release. At expiry flatten BOTH, no carry into next cycle.
BID H1, fixed hypothetical spread/slippage; no real ask, ticks, swap, broker
hedge authorization, margin or assured fills. Already inspected 2023-26.
"""
import csv
import datetime as dt
import hashlib
import json
import math
import sys
from pathlib import Path
from aggressive_range_backtest_h1 import (INITIAL_USD, COSTS, ENTRY_FRACTION,
                                            MAX_TRADES_PER_CYCLE, LOT_UNITS)
from compare_tpo_atr_h1 import HORIZON, collect, range_atr_signal, simple_atr
from range_guard_h1 import load_h1, execution
from trend_h4 import PIP, indicators, load_h4

RISK = .015
# Gross exposure max 4x on initial leg, <=9.2x on 1.3x hedged pair,
# purely research not a margin check or a broker leverage recommendation.
MAIN_NOTIONAL_CAP = 4.0
TARGETS = (.5, .6, .7)
HEDGES = ((1.0, 0), (1.3, 0), (1.3, 10))


def hedge_units(main_units, main_fill, hedge_fill, usd_ratio):
    """Value in USD is units_EUR * USD_per_EUR at EACH actual fill."""
    if min(main_units, main_fill, hedge_fill, usd_ratio) <= 0:
        raise ValueError('USD notional inputs must be positive')
    return main_units * main_fill * usd_ratio / hedge_fill


def one_cycle(bars, ratios, lower, upper, balance, peak, worst_dd,
              target_fraction, ratio, release_offset, spread, slip):
    if len(bars) != HORIZON or len(ratios) != HORIZON or not lower < upper:
        raise ValueError('Invalid 12 hour cycle')
    width = upper-lower
    active = True
    pos = None
    entries = 0
    start_balance = balance
    events = {k:0 for k in ('entries','targets','main_expiry','hedge_opened',
        'hedge_released','hedge_expired','hedge_no_return','ambiguous_rebreach',
        'skipped_reward','skipped_size','paused_boundary','paused_atr','resumed',
        'session_block','gap_beyond_boundary')}
    completed = []
    ledger = []

    def log(when, action, side, bid, fill, units, pnl=0):
        ledger.append({'utc':when.isoformat(),'action':action,
            'side':'long' if side==1 else 'short','bid':round(bid,6),
            'fill':round(fill,6),'units_eur':round(units,6),
            'realized_usd':round(pnl,5)})

    def equity(bid):
        if pos is None:
            return balance
        side=pos['side']; u=pos['units']
        total=balance+side*u*(execution(bid,-side,spread,slip)-pos['entry'])
        if pos['hedged']:
            total-=side*pos['hedge_units']*(execution(bid,side,spread,slip)-pos['hedge_entry'])
        return total

    def mark(bid):
        nonlocal peak,worst_dd
        value=equity(bid)
        if value>peak:peak=value
        if peak>0:worst_dd=max(worst_dd,(peak-value)/peak)

    def finish_main(bid, reason, when):
        nonlocal pos,balance
        side=pos['side'];u=pos['units']
        fill=execution(bid,-side,spread,slip)
        pnl=side*u*(fill-pos['entry'])
        balance+=pnl;pos['lifetime']+=pnl
        log(when,reason,-side,bid,fill,u,pnl)
        completed.append({'opened':pos['opened'].isoformat(),
            'closed':when.isoformat(),'total_pnl_usd':round(pos['lifetime'],5),
            'reason':reason,'hedge_count':pos['hedges']})
        events[reason]+=1
        pos=None
        mark(bid)

    for j,(t,op,hi,lo,cl) in enumerate(bars):
        end=t+dt.timedelta(hours=1)
        just_released=False
        if pos is not None and pos['hedged'] and j>pos['hedge_hour']:
            side=pos['side'];level=pos['release_level']
            prior_close=bars[j-1][4]
            ready=(prior_close<level if side==1 else prior_close>level)
            crosses=(hi>=level if side==1 else lo<=level)
            if ready and crosses:
                # Gap beyond level executed at the opening price, not a free fill.
                at=op if (op>=level if side==1 else op<=level) else level
                fill=execution(at,side,spread,slip)
                pnl=-side*pos['hedge_units']*(fill-pos['hedge_entry'])
                balance+=pnl;pos['lifetime']+=pnl
                log(t,'release_hedge',side,at,fill,pos['hedge_units'],pnl)
                pos['hedged']=False
                events['hedge_released']+=1
                just_released=True
                if (lo<=lower if side==1 else hi>=upper):
                    events['ambiguous_rebreach']+=1
                mark(at)

        if pos is None and active and lower<=op<=upper and entries<MAX_TRADES_PER_CYCLE:
            # Only realized cash is compared with this limit. No fake margin safety.
            if balance-start_balance <= -.03*start_balance:
                events['session_block']+=1
            else:
                side=(1 if op<=lower+ENTRY_FRACTION*width else
                      -1 if op>=upper-ENTRY_FRACTION*width else 0)
                if side:
                    main_fill=execution(op,side,spread,slip)
                    boundary_bid=lower if side==1 else upper-spread*PIP
                    stop_proxy=execution(boundary_bid,-side,spread,slip)
                    risk_unit=side*(main_fill-stop_proxy)
                    tp=lower+(target_fraction if side==1 else 1-target_fraction)*width
                    net_reward=side*(execution(tp,-side,spread,slip)-main_fill)
                    prior_atr=ratios[j][1]
                    # Lower TP requires an adjusted gate: prior 1.25 R gate
                    # mechanically rejected midpoint targets. Reject only
                    # non-positive net reward, tiny stops and unavailable ATR.
                    if (prior_atr is None or risk_unit<=0 or risk_unit<prior_atr
                            or net_reward<=0):
                        events['skipped_reward']+=1
                    else:
                        by_risk=math.floor(balance*RISK/risk_unit/LOT_UNITS)*LOT_UNITS
                        by_notional=math.floor(balance*MAIN_NOTIONAL_CAP/main_fill/LOT_UNITS)*LOT_UNITS
                        units=int(min(by_risk,by_notional))
                        if units<LOT_UNITS:
                            events['skipped_size']+=1
                        else:
                            pos={'side':side,'units':units,'entry':main_fill,
                                 'opened':t,'lifetime':0.,'hedges':0,
                                 'hedged':False,'hedge_units':0.,'hedge_hour':None}
                            entries+=1;events['entries']+=1
                            log(t,'open_main',side,op,main_fill,units)
                            mark(op)

        if pos is not None and not pos['hedged'] and not just_released:
            side=pos['side']
            boundary_bid=lower if side==1 else upper-spread*PIP
            breach=(lo<=boundary_bid if side==1 else hi>=boundary_bid)
            tp=lower+(target_fraction if side==1 else 1-target_fraction)*width
            hit_target=(hi>=tp if side==1 else lo<=tp)
            if breach:
                at=(min(op,boundary_bid) if side==1 else max(op,boundary_bid))
                if (op<boundary_bid if side==1 else op>boundary_bid):
                    events['gap_beyond_boundary']+=1
                mark(at)
                fill=execution(at,-side,spread,slip)
                hu=hedge_units(pos['units'],pos['entry'],fill,ratio)
                pos['hedged']=True;pos['hedge_entry']=fill
                pos['hedge_units']=hu;pos['hedges']+=1
                pos['hedge_hour']=j
                pos['release_level']=((lower-release_offset*PIP) if side==1 else
                                      (upper-spread*PIP+release_offset*PIP))
                events['hedge_opened']+=1
                log(end,'open_hedge',-side,at,fill,hu)
                mark(at)
            elif hit_target:
                mark(lo if side==1 else hi)
                finish_main(tp,'targets',end)
            else:
                mark(lo if side==1 else hi)
        if pos is not None:
            # Once 1.3x hedged, net delta is SHORT against a long main;
            # adverse price direction reverses during the hedge.
            adverse=(hi if pos['hedged'] and ratio>1 else
                     (lo if pos['side']==1 else hi))
            mark(adverse)
            mark(cl)

        breached=lo<lower or hi>upper
        if breached:
            if active:events['paused_boundary']+=1
            active=False
        elif active:
            if ratios[j][0] is None or ratios[j][0]>=5:
                active=False;events['paused_atr']+=1
        elif ratios[j][0] is not None and ratios[j][0]<5 and j<HORIZON-1:
            active=True;events['resumed']+=1

    if pos is not None:
        end=bars[-1][0]+dt.timedelta(hours=1)
        bid=bars[-1][4]
        if pos['hedged']:
            side=pos['side'];hu=pos['hedge_units']
            fill=execution(bid,side,spread,slip)
            pnl=-side*hu*(fill-pos['hedge_entry'])
            balance+=pnl;pos['lifetime']+=pnl
            log(end,'expire_hedge',side,bid,fill,hu,pnl)
            events['hedge_expired']+=1;events['hedge_no_return']+=1
            pos['hedged']=False
        finish_main(bid,'main_expiry',end)
    assert pos is None and entries<=MAX_TRADES_PER_CYCLE
    return balance,peak,worst_dd,events,ledger,completed


def evaluate(h1, episodes, target, ratio, offset, spread, slip):
    by_time={row[0]:i for i,row in enumerate(h1)}
    balance=peak=INITIAL_USD;dd=0.;totals={};ledger=[];positions=[]
    for ep in episodes:
        if not ep['range_atr']:continue
        i=by_time[ep['time']]
        before=[(range_atr_signal(h1[i-80:i+j+1]),
                 simple_atr(h1[i-80:i+j])) for j in range(HORIZON)]
        balance,peak,dd,ev,actions,completed=one_cycle(
            h1[i:i+HORIZON],before,ep['lower'],ep['upper'],
            balance,peak,dd,target,ratio,offset,spread,slip)
        for k,v in ev.items():totals[k]=totals.get(k,0)+v
        ledger.extend({'cycle_utc':ep['time'].isoformat(),**row} for row in actions)
        positions.extend({'cycle_utc':ep['time'].isoformat(),**row} for row in completed)
    win=sum(p['total_pnl_usd']>0 for p in positions)
    loss=sum(p['total_pnl_usd']<0 for p in positions)
    return {'initial_usd':INITIAL_USD,'final_usd':round(balance,2),
            'return_pct':round((balance/INITIAL_USD-1)*100,2),
            'max_worst_hourly_drawdown_pct':round(dd*100,2),
            'positions':len(positions),'winning_positions':win,
            'losing_positions':loss,'win_rate_pct':round(100*win/len(positions),2) if positions else None,
            'events':totals},ledger,positions


def self_test():
    assert abs(hedge_units(1000,1.1001,1.0899,1.3)*1.0899-1.3*1100.1)<1e-7
    assert abs(hedge_units(1000,1.1001,1.0899,1)*1.0899-1100.1)<1e-7
    # The oversized hedge creates net SHORT exposure, not a risk-free lock.
    hu=hedge_units(1000,1.1,1.09,1.3)
    assert hu>1000
    net=lambda price:1000*(price-1.1)-hu*(price-1.09)
    assert net(1.08)>net(1.09) and net(1.10)<net(1.09)
    print('USD-notional hedge sizing and net-delta self-tests OK')


def main():
    if len(sys.argv)==2 and sys.argv[1]=='--self-test':
        self_test();return
    if len(sys.argv)!=2:raise SystemExit('Usage: script data/eurusd_h1.csv')
    path=Path(sys.argv[1]);h1=load_h1(path);h4=load_h4(path)
    _,_,atr,_=indicators(h4)
    split=int(len(h4)*.7);split_time=h4[split][0]
    end=h4[-1][0]+dt.timedelta(hours=4)
    out={'data_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
         'method':'USD-notional hedge; shorter targets; original code untouched',
         'parameters':{'risk_main':RISK,'main_gross_exposure_cap':MAIN_NOTIONAL_CAP,
                       'hedge_usd_notional_ratios':[1,1.3],
                       'tp_channel_fractions':TARGETS,'release_offsets_pips':[0,10],
                       'spread_slippage_assumptions':COSTS,
                       'hedge_notional':'hedge_units*hedge_actual_fill = ratio*main_units*main_actual_fill',
                       'release':'only after previous closed H1 beyond release level; then return crossing',
                       'expiry':'close both legs at 12th H1 close'},
         'limits':'H1 crossing order and actual broker fills unknown. Release/rebreach in one H1 is ambiguous and counted. One daily midnight sample, 12h windows, old 2023-26 inspected. Hypothetical BID-only fixed spreads, no commission, swap, margin and broker hedging validation. Research only, no orders or deployment.',
         'segments':{}}
    Path('reports').mkdir(exist_ok=True)
    for name,start,finish in (('development',h4[200][0],split_time),
                              ('previously_inspected_2023_2026',split_time,end)):
        episodes,audit=collect(h1,h4,atr,start,finish)
        segment={'audit':audit,'variants':{}}
        for target in TARGETS:
            for ratio,offset in HEDGES:
                for cost,(spread,slip) in COSTS.items():
                    key=f'tp{int(target*100)}_usdhedge{int(ratio*100)}_release{offset}pip_{cost}'
                    metrics,actions,positions=evaluate(h1,episodes,target,ratio,offset,spread,slip)
                    segment['variants'][key]=metrics
                    print(name,key,json.dumps(metrics,sort_keys=True))
                    if cost=='base':
                        for suffix,rows in (('actions',actions),('positions',positions)):
                            file=Path('reports')/f'hedge_usd_{name}_{key}_{suffix}.csv'
                            with file.open('w',newline='') as f:
                                if rows:
                                    w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
        out['segments'][name]=segment
    (Path('reports')/'usd_hedge_closer_tp.json').write_text(json.dumps(out,indent=2)+'\n')
    print('RESEARCH ONLY. No live orders. Not a broker-feasible or independently validated edge.')


if __name__=='__main__':main()
