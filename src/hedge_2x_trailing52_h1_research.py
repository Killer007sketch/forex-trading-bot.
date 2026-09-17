"""Research ONLY: EURUSD 12h USD-notional hedge and protected trailing.

Main opens at a known H1 open. The boundary opens a 1x or 2x opposite
USD-NOTIONAL hedge, never closes the main. Hedge has an economic break-even
STOP: sell-fill/buy-fill spread and slippage are included. Starting NEXT H1,
a touch of the stop necessarily closes the hedge, even if the H1 also breached
the boundary; a same-hour rehedge is not assumed (unknown event ordering).
Count such ambiguous bars. Last-bar hedge reversal timing remains unknown.

The 52% level is a protected stop FLOOR, NOT an intrabar guaranteed TP: arm
only when a CLOSED H1 reaches 52% channel and use the next bar. Trailing
stop ratchets from completed closes by 2% of CHANNEL WIDTH. At 12h exit all.
No live orders, commissions, actual broker margin, real ASK/ticks or swaps.
2016-26 data, inspected post-2023 sample NOT a fresh independent holdout.
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

RISK_REFERENCE = .015
MAIN_NOTIONAL_CAP = 4.
ACTIVATION = .52
TRAIL_GAP = .02


def usd_hedge_units(main_units, main_fill, hedge_fill, ratio):
    if min(main_units, main_fill, hedge_fill, ratio) <= 0:
        raise ValueError('Expected positive units, fills and ratio')
    return main_units * main_fill * ratio / hedge_fill


def break_even_bid(hedge_side, fill, spread, slip):
    if hedge_side == -1:
        return fill - (spread+slip)*PIP   # buy ask to cover short hedge
    if hedge_side == 1:
        return fill + slip*PIP            # sell bid to close long hedge
    raise ValueError('Hedge side must be +/-1')


def cycle(bars, ratios, lower, upper, balance, peak, dd, ratio, trailing, spread, slip):
    if len(bars) != HORIZON or len(ratios) != HORIZON or lower >= upper:
        raise ValueError('Invalid 12h cycle')
    width = upper-lower
    session_start = balance
    active = True
    p = None
    entries = 0
    count = {key:0 for key in ('entries','targets','trailing_armed','trailing_exits',
        'main_expiry','hedges_opened','hedges_stopped_at_breakeven','hedges_expired',
        'rehedges','ambiguous_stop_then_rebreach','hedge_created_in_last_hour',
        'paused_boundary','paused_atr','resumed','skipped','skipped_size','session_block',
        'gap_beyond_boundary')}
    actions, positions = [], []

    def record(time, event, side, bid, fill, units, pnl=0.):
        actions.append({'utc':time.isoformat(),'action':event,
            'side':'long' if side == 1 else 'short','bid':round(bid,7),
            'fill':round(fill,7),'units_eur':round(units,6),
            'realized_usd':round(pnl,5)})

    def equity(bid):
        if p is None:
            return balance
        x = balance + p['side']*p['units']*(execution(bid,-p['side'],spread,slip)-p['entry'])
        if p['hedged']:
            x -= p['side']*p['hedge_units']*(execution(bid,p['side'],spread,slip)-p['hedge_fill'])
        return x

    def mark(bid):
        nonlocal peak,dd
        value = equity(bid)
        peak = max(peak,value)
        if peak > 0:
            dd = max(dd,(peak-value)/peak)

    def close_main(bid, reason, time):
        nonlocal p,balance
        s,u = p['side'],p['units']
        fill = execution(bid,-s,spread,slip)
        pnl = s*u*(fill-p['entry'])
        balance += pnl
        p['pnl'] += pnl
        record(time,reason,-s,bid,fill,u,pnl)
        positions.append({'opened':p['opened'].isoformat(),'closed':time.isoformat(),
            'reason':reason,'total_pnl_usd':round(p['pnl'],5),
            'hedge_count':p['hedge_count'],'hedged_at_expiry':p['hedged_at_expiry']})
        count[reason] += 1
        p = None
        mark(bid)

    for j,(time,op,hi,lo,cl) in enumerate(bars):
        finish = time+dt.timedelta(hours=1)
        released = False
        if p is not None and p['hedged'] and j > p['hedge_hour']:
            hs = -p['side']
            stop = break_even_bid(hs,p['hedge_fill'],spread,slip)
            hit = hi >= stop if hs == -1 else lo <= stop
            if hit:
                # If also a fresh breach, the BE stop STILL must execute.
                # Rehedging later within same H1 is ambiguous and is NOT
                # credited; next H1 may rehedge at adverse opening gap.
                rebreach = lo <= lower if p['side'] == 1 else hi >= upper
                if rebreach:
                    count['ambiguous_stop_then_rebreach'] += 1
                bid = max(op,stop) if hs == -1 else min(op,stop)
                fill = execution(bid,-hs,spread,slip)
                pnl = hs*p['hedge_units']*(fill-p['hedge_fill'])
                balance += pnl
                p['pnl'] += pnl
                record(time,'stop_hedge_be',-hs,bid,fill,p['hedge_units'],pnl)
                p['hedged'] = False
                count['hedges_stopped_at_breakeven'] += 1
                released = True
                mark(bid)

        if p is None and active and lower <= op <= upper and entries < MAX_TRADES_PER_CYCLE:
            if balance-session_start <= -.03*session_start:
                count['session_block'] += 1
            else:
                side = 1 if op <= lower+ENTRY_FRACTION*width else -1 if op >= upper-ENTRY_FRACTION*width else 0
                if side:
                    fill = execution(op,side,spread,slip)
                    boundary = lower if side == 1 else upper-spread*PIP
                    reference_risk = side*(fill-execution(boundary,-side,spread,slip))
                    activation_bid = lower+(ACTIVATION if side == 1 else 1-ACTIVATION)*width
                    reward = side*(execution(activation_bid,-side,spread,slip)-fill)
                    atr = ratios[j][1]
                    if atr is None or reference_risk <= 0 or reference_risk < atr or reward <= 0:
                        count['skipped'] += 1
                    else:
                        by_ref = math.floor(balance*RISK_REFERENCE/reference_risk/LOT_UNITS)*LOT_UNITS
                        by_cap = math.floor(balance*MAIN_NOTIONAL_CAP/fill/LOT_UNITS)*LOT_UNITS
                        units = int(min(by_ref,by_cap))
                        if units < LOT_UNITS:
                            count['skipped_size'] += 1
                        else:
                            p = {'opened':time,'side':side,'units':units,'entry':fill,
                                 'hedged':False,'hedge_units':0.,'hedge_fill':0.,
                                 'hedge_hour':-1,'hedge_count':0,'pnl':0.,
                                 'trailing':False,'trail':None,'hedged_at_expiry':False}
                            entries += 1
                            count['entries'] += 1
                            record(time,'open_main',side,op,fill,units)
                            mark(op)

        if p is not None and not p['hedged'] and not released:
            side = p['side']
            boundary = lower if side == 1 else upper-spread*PIP
            activation_bid = lower+(ACTIVATION if side == 1 else 1-ACTIVATION)*width
            ts = p['trail']
            if p['trailing'] and (lo <= ts if side == 1 else hi >= ts):
                bid = min(op,ts) if side == 1 else max(op,ts)
                mark(bid)
                close_main(bid,'trailing_exits',finish)
            elif lo <= boundary if side == 1 else hi >= boundary:
                bid = min(op,boundary) if side == 1 else max(op,boundary)
                if op < boundary if side == 1 else op > boundary:
                    count['gap_beyond_boundary'] += 1
                mark(bid)
                hedge_fill = execution(bid,-side,spread,slip)
                hu = usd_hedge_units(p['units'],p['entry'],hedge_fill,ratio)
                if p['hedge_count']:
                    count['rehedges'] += 1
                p['hedged'] = True
                p['hedge_fill'] = hedge_fill
                p['hedge_units'] = hu
                p['hedge_hour'] = j
                p['hedge_count'] += 1
                count['hedges_opened'] += 1
                if j == HORIZON-1:
                    count['hedge_created_in_last_hour'] += 1
                record(finish,'open_hedge',-side,bid,hedge_fill,hu)
                mark(bid)
            elif not trailing and (hi >= activation_bid if side == 1 else lo <= activation_bid):
                bid = max(op,activation_bid) if side == 1 else min(op,activation_bid)
                mark(bid)
                close_main(bid,'targets',finish)
            else:
                mark(lo if side == 1 else hi)
                if trailing and (cl >= activation_bid if side == 1 else cl <= activation_bid):
                    if not p['trailing']:
                        p['trailing'] = True
                        count['trailing_armed'] += 1
                    proposed = (max(activation_bid,cl-TRAIL_GAP*width) if side == 1 else
                                min(activation_bid,cl+TRAIL_GAP*width))
                    if p['trail'] is None:
                        p['trail'] = proposed
                    else:
                        p['trail'] = max(p['trail'],proposed) if side == 1 else min(p['trail'],proposed)

        if p is not None:
            mark(lo)
            mark(hi)
            mark(cl)
        breached = lo < lower or hi > upper
        if breached:
            if active:
                count['paused_boundary'] += 1
            active = False
        elif active:
            if ratios[j][0] is None or ratios[j][0] >= 5:
                count['paused_atr'] += 1
                active = False
        elif ratios[j][0] is not None and ratios[j][0] < 5 and j < HORIZON-1:
            active = True
            count['resumed'] += 1

    if p is not None:
        end = bars[-1][0]+dt.timedelta(hours=1)
        bid = bars[-1][4]
        if p['hedged']:
            p['hedged_at_expiry'] = True
            hs = -p['side']
            fill = execution(bid,-hs,spread,slip)
            pnl = hs*p['hedge_units']*(fill-p['hedge_fill'])
            balance += pnl
            p['pnl'] += pnl
            record(end,'expire_hedge',-hs,bid,fill,p['hedge_units'],pnl)
            count['hedges_expired'] += 1
            p['hedged'] = False
        close_main(bid,'main_expiry',end)
    assert p is None and entries <= MAX_TRADES_PER_CYCLE
    return balance,peak,dd,count,actions,positions


def evaluate(h1,episodes,ratio,trailing,spread,slip):
    index = {x[0]:i for i,x in enumerate(h1)}
    balance=peak=INITIAL_USD
    dd=0.
    total={}
    positions=[]
    actions=[]
    for ep in episodes:
        if not ep['range_atr']:
            continue
        i = index[ep['time']]
        pre = [(range_atr_signal(h1[i-80:i+j+1]),simple_atr(h1[i-80:i+j])) for j in range(HORIZON)]
        balance,peak,dd,ev,ledger,trades=cycle(h1[i:i+HORIZON],pre,ep['lower'],ep['upper'],
                                             balance,peak,dd,ratio,trailing,spread,slip)
        for k,v in ev.items():
            total[k]=total.get(k,0)+v
        actions += [{'cycle':ep['time'].isoformat(),**x} for x in ledger]
        positions += [{'cycle':ep['time'].isoformat(),**x} for x in trades]
    winners=sum(x['total_pnl_usd']>0 for x in positions)
    details={}
    groups={
        'profit_exit':[x for x in positions if x['reason']!='main_expiry'],
        'expiry_ever_hedged':[x for x in positions if x['reason']=='main_expiry' and x['hedge_count']>0],
        'expiry_never_hedged':[x for x in positions if x['reason']=='main_expiry' and x['hedge_count']==0]}
    for key,group in groups.items():
        details[key]={'n':len(group),'pnl_usd':round(sum(x['total_pnl_usd'] for x in group),2),
                      'winners':sum(x['total_pnl_usd']>0 for x in group),
                      'losers':sum(x['total_pnl_usd']<0 for x in group)}
    return {'initial_usd':INITIAL_USD,'final_usd':round(balance,2),
        'return_pct':round(100*(balance/INITIAL_USD-1),2),
        'max_worst_h1_drawdown_pct':round(dd*100,2),
        'positions':len(positions),'winners':winners,'losers':sum(x['total_pnl_usd']<0 for x in positions),
        'breakdown':details,'events':total},positions,actions


def self_test():
    u=usd_hedge_units(1000,1.1,1.09,2.)
    assert abs(u*1.09-2200)<1e-8
    for side,entry in [(-1,1.08997),(1,1.09018)]:
        be=break_even_bid(side,entry,1.5,.3)
        assert abs(side*(execution(be,-side,1.5,.3)-entry))<1e-10
    start=dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)
    bars=[]
    for j in range(12):
        op,hi,lo,cl=(1.10,1.11,1.095,1.10)
        if j==1:op,hi,lo,cl=(1.10,1.10,1.085,1.085)
        if j==2:op,hi,lo,cl=(1.085,1.092,1.084,1.091)
        if j>2:op,hi,lo,cl=(1.10,1.11,1.095,1.10)
        bars.append((start+dt.timedelta(hours=j),op,hi,lo,cl))
    _,_,_,ev,ledger,_=cycle(bars,[(4.,.001)]*12,1.09,1.29,10000.,10000.,0.,2.,True,1.5,.3)
    assert ev['hedges_opened']>=1 and ev['hedges_stopped_at_breakeven']>=1,ev
    assert any(x['action']=='stop_hedge_be' for x in ledger)
    print('BE hedge stop executes on touched next H1, 2x USD nominal and zero-PnL fill tests PASS')


def main():
    if len(sys.argv)==2 and sys.argv[1]=='--self-test':
        self_test();return
    if len(sys.argv)!=2:
        raise SystemExit('Usage: python src/hedge_2x_trailing52_h1_research.py data/eurusd_h1.csv')
    path=Path(sys.argv[1]);h1=load_h1(path);h4=load_h4(path)
    _,_,h4atr,_=indicators(h4)
    split=int(len(h4)*.7)
    spans=[('development',h4[200][0],h4[split][0]),
           ('previously_inspected_2023_2026',h4[split][0],h4[-1][0]+dt.timedelta(hours=4))]
    report={'data_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'version':'Corrected BE STOP executes on touched next H1; replaces invalid earlier run where stop never executed',
        'tp':'52% channel fixed target comparator versus 52% closed-H1 activation, 52% price floor and 2% channel-width close-only trailing next bar',
        'hedge':'1x or 2x initial USD nominal measured at actual fills, BE hedge stop includes fixed spread/slippage; main never stopped at boundary; both close at 12h',
        'risk':'1.5% initial boundary reference only, NOT actual guaranteed max risk; main notional cap 4x balance, hedged gross up to about 12x; margin/liquidation NOT modeled',
        'limits':'BID H1 OHLC cannot timestamp same-hour hedge opening, hedge return/rebreach, TP activation or gaps. BE stops after hedge creation only from next H1; no same-hour rehedge credited. Old 2023-26 repeatedly inspected, not independent. Hypothetical fixed costs, no commission, swaps, variable spreads, real ASK, tick execution or real broker permission. NO orders.',
        'segments':{}}
    Path('reports').mkdir(exist_ok=True)
    for name,begin,end in spans:
        episodes,audit=collect(h1,h4,h4atr,begin,end)
        out={'audit':audit,'variants':{}}
        for multiple,trailing in [(1.,False),(2.,False),(1.,True),(2.,True)]:
            for cost,(spread,slip) in COSTS.items():
                key=f'hedge{int(multiple)}x_{"trailing52_2" if trailing else "fixed52"}_{cost}'
                result,positions,actions=evaluate(h1,episodes,multiple,trailing,spread,slip)
                out['variants'][key]=result
                print(name,key,json.dumps(result,sort_keys=True))
                if cost=='base':
                    for suffix,rows in [('positions',positions),('actions',actions)]:
                        with (Path('reports')/f'hedge2_trail_{name}_{key}_{suffix}.csv').open('w',newline='') as f:
                            if rows:
                                writer=csv.DictWriter(f,fieldnames=list(rows[0]))
                                writer.writeheader();writer.writerows(rows)
        report['segments'][name]=out
    (Path('reports')/'hedge2_trailing52_report.json').write_text(json.dumps(report,indent=2)+'\n')
    print('RESEARCH ONLY; no broker trading or profitability guarantee.')


if __name__=='__main__':
    main()
