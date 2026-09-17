"""RESEARCH ONLY: matched 12h equal-size hedge vs stop/cash/re-entry.

Prior CLOSED H1 only for signals. A main position is opened at an H1 open.
When an outer boundary is hit, hedge mode opens an equal opposing leg; cash
mode closes the main at THE SAME bid quote. On return to that boundary hedge
mode closes only the hedge; cash mode reopens the original main at THE SAME
quote. Other entries are forbidden while locked/cash. At hour 12 close ALL
open legs. This isolates the economics of the proposed hedge. In H1 candles
intrabar sequence is unknowable: breach always precedes TP; hedge return may
precede a second excursion in the same candle. Count such ambiguous candles,
do NOT claim executable returns or live profitability. Bid-only hypothetical
spread/slip, no broker commissions, swap, margin or actual locked hedging.
"""
import csv
import datetime as dt
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path

from aggressive_range_backtest_h1 import (INITIAL_USD, RISK_LEVELS, COSTS,
    MAX_NOTIONAL_TO_EQUITY, MAX_REALIZED_SESSION_LOSS, MAX_TRADES_PER_CYCLE,
    ENTRY_FRACTION, TARGET_FRACTION, LOT_UNITS, run_segment as original_stop_run)
from compare_tpo_atr_h1 import HORIZON, collect, range_atr_signal, simple_atr
from range_guard_h1 import execution, load_h1
from trend_h4 import PIP, indicators, load_h4

MODES = ('equal_hedge', 'stop_and_reenter')


def cycle(window, ratios, lower, upper, balance, peak, drawdown, risk, spread, slip, mode):
    if mode not in MODES or len(window) != HORIZON or len(ratios) != HORIZON or lower >= upper:
        raise ValueError('Bad mode, range or 12h window')
    width = upper-lower
    spread_px = spread*PIP
    session_start = balance
    active = True
    p = None
    entries = 0
    events = {k:0 for k in ('entries','targets','main_expiry','hedges_opened',
        'hedges_released','hedges_expired','cash_stops','cash_reentries',
        'cash_expired_unreturned','boundary_pauses','ratio_pauses','resumes',
        'skipped_rr_atr','skipped_size','session_loss_blocks','breach_hours',
        'ambiguous_return_and_rebreach_same_h1','gap_adverse_breaches')}
    ledger = []
    completed = []

    def record(t, kind, side, bid, price, units, pnl=0):
        ledger.append({'utc':t.isoformat(),'action':kind,
            'direction':'long' if side==1 else 'short','bid_reference':round(bid,6),
            'fill':round(price,6),'units_eur':units,'realized_usd':round(pnl,5),
            'mode':mode})

    def mark_equity(bid):
        if p is None or p['state']=='cash':
            return balance
        main = p['side']*p['units']*(execution(bid,-p['side'],spread,slip)-p['entry'])
        if p['state']=='hedged':
            other = -p['side']*p['units']*(execution(bid,p['side'],spread,slip)-p['hedge_entry'])
            return balance+main+other
        return balance+main

    def note_mark(equity):
        nonlocal peak,drawdown
        peak=max(peak,equity)
        if peak > 0:
            drawdown=max(drawdown,(peak-equity)/peak)

    def close_main(bid, reason, when):
        nonlocal balance,p
        assert p is not None and p['state']=='open'
        fill=execution(bid,-p['side'],spread,slip)
        pnl=p['side']*p['units']*(fill-p['entry'])
        balance+=pnl
        p['life_pnl']+=pnl
        record(when,reason,-p['side'],bid,fill,p['units'],pnl)
        completed.append({'opened':p['opened'].isoformat(),'closed':when.isoformat(),
            'result_usd':round(p['life_pnl'],5),'reason':reason,
            'hedge_roundtrips':p['hedges'],'units_eur':p['units']})
        events[reason]+=1
        p=None
        note_mark(balance)

    for j,(t,op,hi,lo,cl) in enumerate(window):
        finish=t+dt.timedelta(hours=1)
        just_released=False
        # Existing hedge/cash must be resolved before any new primary order.
        if p is not None and p['state'] in ('hedged','cash'):
            side=p['side']; level=lower if side==1 else upper-spread_px
            # Crossing back to the SAME bid quote (upper for short is ask-adjusted).
            returned = op>=level or hi>=level if side==1 else op<=level or lo<=level
            if returned:
                ret_bid=op if ((op>=level) if side==1 else (op<=level)) else level
                fill=execution(ret_bid,side,spread,slip)
                if mode=='equal_hedge':
                    pnl=-side*p['units']*(fill-p['hedge_entry'])
                    balance+=pnl; p['life_pnl']+=pnl
                    record(t,'release_hedge',side,ret_bid,fill,p['units'],pnl)
                    events['hedges_released']+=1
                else:
                    p['entry']=fill
                    record(t,'reenter_main',side,ret_bid,fill,p['units'])
                    events['cash_reentries']+=1
                p['state']='open';p['hedge_entry']=None
                just_released=True
                # H1 cannot order a rebreach after a same-candle return.
                if (lo<lower if side==1 else hi>upper):
                    events['ambiguous_return_and_rebreach_same_h1']+=1
                note_mark(mark_equity(ret_bid))

        if p is None and active and lower<=op<=upper and entries<MAX_TRADES_PER_CYCLE:
            if balance-session_start <= -MAX_REALIZED_SESSION_LOSS*session_start:
                events['session_loss_blocks']+=1
            else:
                side=1 if op<=lower+ENTRY_FRACTION*width else -1 if op>=upper-ENTRY_FRACTION*width else 0
                if side:
                    enter=execution(op,side,spread,slip)
                    stop_bid=lower if side==1 else upper-spread_px
                    hypothetical_stop=execution(stop_bid,-side,spread,slip)
                    r=side*(enter-hypothetical_stop)
                    tp_bid=lower+(TARGET_FRACTION if side==1 else 1-TARGET_FRACTION)*width
                    reward=side*(execution(tp_bid,-side,spread,slip)-enter)
                    atr=ratios[j][1]
                    if atr is None or r<atr or r<=0 or reward<=1.25*r:
                        events['skipped_rr_atr']+=1
                    else:
                        u_r=math.floor((balance*risk/r)/LOT_UNITS)*LOT_UNITS
                        u_n=math.floor((balance*MAX_NOTIONAL_TO_EQUITY/enter)/LOT_UNITS)*LOT_UNITS
                        units=int(min(u_r,u_n))
                        if units<LOT_UNITS:
                            events['skipped_size']+=1
                        else:
                            p={'side':side,'units':units,'entry':enter,'opened':t,
                               'state':'open','hedge_entry':None,'life_pnl':0.,'hedges':0}
                            entries+=1;events['entries']+=1
                            record(t,'open_main',side,op,enter,units)
                            note_mark(mark_equity(op))

        # For active primary legs the adverse boundary wins over intrabar TP.
        # No same-hour TP/rehedge after a hedge release: H1 chronology unknown.
        if p is not None and p['state']=='open' and not just_released:
            side=p['side'];level=lower if side==1 else upper-spread_px
            breach=lo<=level if side==1 else hi>=level
            tp_bid=lower+(TARGET_FRACTION if side==1 else 1-TARGET_FRACTION)*width
            target=hi>=tp_bid if side==1 else lo<=tp_bid
            if breach:
                bid=min(op,level) if side==1 else max(op,level)
                if (op<level if side==1 else op>level):
                    events['gap_adverse_breaches']+=1
                note_mark(mark_equity(bid))
                fill=execution(bid,-side,spread,slip)
                if mode=='equal_hedge':
                    p['hedge_entry']=fill;p['state']='hedged';p['hedges']+=1
                    events['hedges_opened']+=1
                    record(finish,'open_equal_hedge',-side,bid,fill,p['units'])
                else:
                    pnl=side*p['units']*(fill-p['entry'])
                    balance+=pnl;p['life_pnl']+=pnl;p['state']='cash'
                    events['cash_stops']+=1
                    record(finish,'stop_main_wait_reentry',-side,bid,fill,p['units'],pnl)
            elif target:
                note_mark(mark_equity(lo if side==1 else hi))
                close_main(tp_bid,'targets',finish)
            else:
                note_mark(mark_equity(lo if side==1 else hi))
        if p is not None:
            note_mark(mark_equity(cl))

        outer_breach=lo<lower or hi>upper
        if outer_breach:
            events['breach_hours']+=1
            if active:events['boundary_pauses']+=1
            active=False
        elif active:
            if ratios[j][0] is None or ratios[j][0]>=5:
                events['ratio_pauses']+=1;active=False
        elif ratios[j][0] is not None and ratios[j][0]<5 and j<HORIZON-1:
            active=True;events['resumes']+=1

    if p is not None:
        end=window[-1][0]+dt.timedelta(hours=1)
        last=window[-1][4]
        if p['state']=='hedged':
            # Two separate market exits; both spreads/slippage paid.
            side=p['side'];u=p['units']
            sell_main=execution(last,-side,spread,slip)
            buy_hedge=execution(last,side,spread,slip)
            pnl_main=side*u*(sell_main-p['entry'])
            pnl_hedge=-side*u*(buy_hedge-p['hedge_entry'])
            balance+=pnl_main+pnl_hedge;p['life_pnl']+=pnl_main+pnl_hedge
            record(end,'close_main_hedged_expiry',-side,last,sell_main,u,pnl_main)
            record(end,'close_hedge_expiry',side,last,buy_hedge,u,pnl_hedge)
            events['hedges_expired']+=1
            reason='hedged_expiry'
        elif p['state']=='cash':
            events['cash_expired_unreturned']+=1
            reason='cash_expiry'
        else:
            fill=execution(last,-p['side'],spread,slip)
            pnl=p['side']*p['units']*(fill-p['entry'])
            balance+=pnl;p['life_pnl']+=pnl
            record(end,'main_expiry',-p['side'],last,fill,p['units'],pnl)
            events['main_expiry']+=1
            reason='main_expiry'
        completed.append({'opened':p['opened'].isoformat(),'closed':end.isoformat(),
            'result_usd':round(p['life_pnl'],5),'reason':reason,
            'hedge_roundtrips':p['hedges'],'units_eur':p['units']})
        p=None;note_mark(balance)
    assert p is None and entries<=MAX_TRADES_PER_CYCLE
    return balance,peak,drawdown,events,ledger,completed


def run_segment(h1, episodes, risk, spread, slip, mode):
    by_time={bar[0]:i for i,bar in enumerate(h1)}
    balance=peak=INITIAL_USD;dd=0.
    totals={};ledger=[];completed=[];cycles_traded=0
    for ep in episodes:
        if not ep['range_atr']:continue
        i=by_time[ep['time']]
        ratios=[(range_atr_signal(h1[i-80:i+j+1]),
                 simple_atr(h1[i-80:i+j])) for j in range(HORIZON)]
        balance,peak,dd,ev,rows,done=cycle(h1[i:i+HORIZON],ratios,ep['lower'],
            ep['upper'],balance,peak,dd,risk,spread,slip,mode)
        cycles_traded+=bool(done)
        for key,value in ev.items():totals[key]=totals.get(key,0)+value
        for row in rows:ledger.append({'cycle_utc':ep['time'].isoformat(),**row})
        for row in done:completed.append({'cycle_utc':ep['time'].isoformat(),**row})
    wins=[x['result_usd'] for x in completed if x['result_usd']>0]
    losses=[-x['result_usd'] for x in completed if x['result_usd']<0]
    return {'initial_usd':INITIAL_USD,'final_usd':round(balance,2),
        'net_pnl_usd':round(balance-INITIAL_USD,2),
        'return_pct':round((balance/INITIAL_USD-1)*100,2),
        'max_intrabar_worst_drawdown_pct':round(dd*100,2),
        'flat_signals':sum(ep['range_atr'] for ep in episodes),
        'cycles_with_primary_trades':cycles_traded,'completed_primary_positions':len(completed),
        'positive_primary_positions':len(wins),'negative_primary_positions':len(losses),
        'win_rate_pct':round(100*len(wins)/len(completed),2) if completed else None,
        'profit_factor':round(sum(wins)/sum(losses),3) if losses else None,
        'events':totals},ledger,completed


def main():
    if len(sys.argv)!=2:
        raise SystemExit('Usage: python src/hedge_return_12h_research.py data/eurusd_h1.csv')
    path=Path(sys.argv[1]);h1=load_h1(path);h4=load_h4(path)
    _,_,h4_atr,_=indicators(h4)
    split=int(len(h4)*.7);split_time=h4[split][0]
    end=h4[-1][0]+dt.timedelta(hours=4)
    out={'data_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'parameters':{'start_usd':INITIAL_USD,'risk_per_primary':RISK_LEVELS,
        'costs_spread_slippage_pips':COSTS,'max_notional_to_equity':MAX_NOTIONAL_TO_EQUITY,
        'max_entries_per_cycle':MAX_TRADES_PER_CYCLE,
        'entry_zone_fraction':ENTRY_FRACTION,'target_fraction':TARGET_FRACTION,
        'outer_boundary':'long bid lower, short ask upper (bid upper-spread)',
        'hedge':'equal EUR units, no stop on boundary, open at boundary or worse gap; close hedge ONLY at same quote on return',
        'cash_comparator':'close initial main at identical boundary quote; re-enter same EUR units at exact hedge-close quote',
        'at_12h':'flatten all open hedge and main legs; comparator stays cash if no return',
        'priority':'boundary hedge before TP if both on one H1; skip any TP/rehedge on hour of hedge release; crossings inside H1 unresolved'},
        'limitations':'BID-only H1: return may rebreach after recovery IN SAME HOUR, unknown; counted but not resolved and can bias PnL. Intrahour entry/pause latency unverified. Fixed hypothetical spread/slip, no broker commissions, swaps, variable spreads, guaranteed fills, financing, hedged account feasibility, margin calls or netting rules. 10x exposure cap is research not broker margin. Same previously inspected 2023-26, no independent out-of-sample. One midnight cycle/day, no continuous bot. Underlying prior static entry and distant 70pct range target remain; hedge alone cannot establish profitability. No trading.' ,'segments':{}}
    Path('reports').mkdir(exist_ok=True)
    for name,start,finish in (('development',h4[200][0],split_time),
                              ('previously_inspected_2023_2026',split_time,end)):
        episodes,audit=collect(h1,h4,h4_atr,start,finish)
        segment={'audit':audit,'variants':{}}
        for risk_name,risk in RISK_LEVELS.items():
            for cost_name,(spread,slip) in COSTS.items():
                baseline,baseline_ledger=original_stop_run(h1,episodes,risk,spread,slip)
                segment['variants'][risk_name+'_'+cost_name+'_old_stop']=baseline
                for mode in MODES:
                    result,ledger,completed=run_segment(h1,episodes,risk,spread,slip,mode)
                    key=risk_name+'_'+cost_name+'_'+mode
                    segment['variants'][key]=result
                    with (Path('reports')/f'hedge_12h_{name}_{key}_events.csv').open('w',newline='') as f:
                        fields=['cycle_utc','utc','action','direction','bid_reference','fill','units_eur','realized_usd','mode']
                        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(ledger)
                    with (Path('reports')/f'hedge_12h_{name}_{key}_positions.csv').open('w',newline='') as f:
                        fields=['cycle_utc','opened','closed','result_usd','reason','hedge_roundtrips','units_eur']
                        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(completed)
                print(name,risk_name,cost_name,json.dumps({k:v for k,v in segment['variants'].items() if k.startswith(risk_name+'_'+cost_name)},sort_keys=True))
        out['segments'][name]=segment
    (Path('reports')/'hedge_12h_research.json').write_text(json.dumps(out,indent=2)+'\n')
    print('RESEARCH ONLY; no live trading. Same-boundary hedge vs exit/reentry, ambiguous H1 intrabar rebreaches flagged.')


if __name__=='__main__':main()
