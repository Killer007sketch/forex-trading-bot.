"""Offline Strategy A: selected reclaim20 entry, stop at frozen edge, fixed 3R TP.

Only entry permission expires after 12 hours. An already-open position stays
open until SL or TP, independently of the original daily flat signal. Never
open overlapping positions; count midnight opportunities blocked by a carry.
Compare IDENTICAL 3R rules with and without a 12-hour position time exit.
Original Strategy A and Strategy B scripts are unchanged; no broker orders.

Prices: Dukascopy H1 BID, hypothetical fixed spread/slippage, conservative
SL-first if both touched in one H1, next-open adverse gap execution. No swaps,
commission, margin/liquidation, intrabar sequencing or actual ASK/tick fills.
Previously-inspected 2023-26 is NOT untouched out-of-sample.
"""
import csv
import datetime as dt
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path

from aggressive_range_backtest_h1 import COSTS, INITIAL_USD, LOT_UNITS, MAX_TRADES_PER_CYCLE
from compare_tpo_atr_h1 import HORIZON, collect, range_atr_signal, simple_atr
from range_guard_h1 import execution, load_h1
from trend_h4 import PIP, indicators, load_h4
from strategy_a_entry_optimization_h1 import entry_side

RISK = .015
CAP = 4.0
TARGET_R = 3.0
HOUR = dt.timedelta(hours=1)


def levels(side, entry_fill, lower, upper, spread, slip):
    stop = lower if side == 1 else upper - spread*PIP
    risk = side * (entry_fill-execution(stop,-side,spread,slip))
    if risk <= 0:
        raise ValueError('Nonpositive stop loss distance')
    # The TP is an exact 3x the economic loss at the stop, net of spread and
    # adverse slippage, before any gaps; TP bid is NOT a naive +/-3*bid distance.
    tp_fill = entry_fill + side*TARGET_R*risk
    tp = tp_fill + slip*PIP if side == 1 else tp_fill-(spread+slip)*PIP
    assert abs(side*(execution(tp,-side,spread,slip)-entry_fill)-TARGET_R*risk) < 1e-8
    return stop, tp, risk


def backtest(h1, opportunities, begin, end, rule, spread, slip, time_exit):
    eligible = {x['time']:x for x in opportunities if x['range_atr']}
    balance=peak=INITIAL_USD
    drawdown=0.
    position=None
    day=None
    day_finish=None
    active=False
    day_balance=balance
    opened_day=0
    counters={key:0 for key in ('entries','tp','sl','expiry12h','open_at_end','blocked_midnights',
        'held_over_12h','tp_outside_channel','sl_tp_same_h1','stop_gap','target_gap',
        'skipped_atr_or_reward','skipped_lot','blocked_session','boundary_pause',
        'atr_pause','resume','days_with_signal')}
    trades=[]
    fills=[]
    durations=[]
    def log(t,event,p,bid,fill,pnl=0):
        fills.append({'utc':t.isoformat(),'event':event,'side':p['side'],
            'bid':round(bid,7),'fill':round(fill,7),'units_eur':p['units'],
            'pnl_usd':round(pnl,5),'entry_utc':p['opened'].isoformat()})

    def equity(bid):
        if position is None:
            return balance
        s=position['side']
        return balance+s*position['units']*(execution(bid,-s,spread,slip)-position['entry'])

    def mark(bid):
        nonlocal drawdown
        value=equity(bid)
        if peak>0:
            drawdown=max(drawdown,(peak-value)/peak)

    def exit_at(bid,reason,exit_time):
        nonlocal balance,peak,position,drawdown
        p=position
        s=p['side']
        fill=execution(bid,-s,spread,slip)
        pnl=s*p['units']*(fill-p['entry'])
        balance+=pnl
        peak=max(peak,balance)
        if peak>0:
            drawdown=max(drawdown,(peak-balance)/peak)
        hours=(exit_time-p['opened']).total_seconds()/3600
        durations.append(hours)
        if hours>HORIZON:
            counters['held_over_12h']+=1
        log(exit_time,reason,p,bid,fill,pnl)
        trades.append({'opened_utc':p['opened'].isoformat(),
            'closed_utc':exit_time.isoformat(),'side':s,'units_eur':p['units'],
            'entry_fill':round(p['entry'],7),'stop_bid':round(p['stop'],7),
            'tp_bid':round(p['tp'],7),'R_usd':round(p['units']*p['risk'],5),
            'pnl_usd':round(pnl,5),'reason':reason,'hold_hours':round(hours,2),
            'tp_outside_frozen_channel':p['outside']})
        counters[reason]+=1
        position=None

    previous=None
    for i,bar in enumerate(h1):
        t,op,hi,lo,cl=bar
        if t<begin:
            continue
        if t>=end:
            break
        finish=t+HOUR
        if t.hour==0 and (day is None or day['time']!=t):
            candidate=eligible.get(t)
            if candidate is not None:
                counters['days_with_signal']+=1
                if position is not None:
                    counters['blocked_midnights']+=1
            day=candidate
            day_finish=t+HORIZON*HOUR if day else None
            active=bool(day)
            day_balance=balance
            opened_day=0
        # If there was no midnight quote (e.g. weekend), an old day can never
        # create new orders after the 12-hour opening window.
        in_window=day is not None and day['time']<=t and finish<=day_finish
        closed_this_bar=False
        if position is not None:
            p=position
            s=p['side']
            hit_stop=lo<=p['stop'] if s==1 else hi>=p['stop']
            hit_tp=hi>=p['tp'] if s==1 else lo<=p['tp']
            if hit_stop and hit_tp:
                counters['sl_tp_same_h1']+=1
            if hit_stop:
                price=min(op,p['stop']) if s==1 else max(op,p['stop'])
                if price!=p['stop']:
                    counters['stop_gap']+=1
                mark(price)
                exit_at(price,'sl',finish)
                closed_this_bar=True
            elif hit_tp:
                # No optimistic improvement for favorable gaps: limit TP price.
                price=p['tp']
                if (op>p['tp'] if s==1 else op<p['tp']):
                    counters['target_gap']+=1
                mark(price)
                exit_at(price,'tp',finish)
                closed_this_bar=True
            else:
                mark(lo if s==1 else hi)
                mark(cl)
                if time_exit and finish>=p['opened']+HORIZON*HOUR:
                    exit_at(cl,'expiry12h',finish)
                    closed_this_bar=True
        if position is None and not closed_this_bar and in_window and active and opened_day<MAX_TRADES_PER_CYCLE and day['lower']<=op<=day['upper']:
            if balance-day_balance<=-.03*day_balance:
                counters['blocked_session']+=1
            elif i>=2:
                prior=(range_atr_signal(h1[max(0,i-80):i]),simple_atr(h1[max(0,i-80):i]),h1[i-1],h1[i-2])
                side=entry_side(rule,op,day['lower'],day['upper'],prior)
                if side:
                    entry=execution(op,side,spread,slip)
                    stop,tp,risk=levels(side,entry,day['lower'],day['upper'],spread,slip)
                    activation=day['lower']+(.52 if side==1 else .48)*(day['upper']-day['lower'])
                    reward=side*(execution(activation,-side,spread,slip)-entry)
                    if prior[1] is None or risk<prior[1] or reward<=0:
                        counters['skipped_atr_or_reward']+=1
                    else:
                        byrisk=math.floor(balance*RISK/risk/LOT_UNITS)*LOT_UNITS
                        bycap=math.floor(balance*CAP/entry/LOT_UNITS)*LOT_UNITS
                        units=int(min(byrisk,bycap))
                        if units<LOT_UNITS:
                            counters['skipped_lot']+=1
                        else:
                            outside=tp>day['upper'] if side==1 else tp<day['lower']
                            position={'side':side,'entry':entry,'opened':t,
                                'units':units,'stop':stop,'tp':tp,'risk':risk,
                                'outside':outside}
                            counters['entries']+=1
                            opened_day+=1
                            if outside:counters['tp_outside_channel']+=1
                            log(t,'open',position,op,entry)
                            # Allow an SL/TP in the entry H1, like the original
                            # simulator: the open is known to occur first.
                            hs=lo<=stop if side==1 else hi>=stop
                            ht=hi>=tp if side==1 else lo<=tp
                            if hs and ht:counters['sl_tp_same_h1']+=1
                            if hs:
                                price=min(op,stop) if side==1 else max(op,stop)
                                if price!=stop:counters['stop_gap']+=1
                                mark(price)
                                exit_at(price,'sl',finish)
                                closed_this_bar=True
                            elif ht:
                                mark(tp)
                                exit_at(tp,'tp',finish)
                                closed_this_bar=True
                            else:
                                mark(lo if side==1 else hi)
                                mark(cl)
        if in_window:
            outside=lo<day['lower'] or hi>day['upper']
            if outside:
                if active:counters['boundary_pause']+=1
                active=False
            elif active:
                ratio=range_atr_signal(h1[max(0,i-79):i+1])
                if ratio is None or ratio>=5:
                    counters['atr_pause']+=1
                    active=False
            else:
                ratio=range_atr_signal(h1[max(0,i-79):i+1])
                if ratio is not None and ratio<5 and finish<day_finish:
                    counters['resume']+=1
                    active=True
        previous=t

    end_bar=next((bar for bar in reversed(h1) if begin<=bar[0]<end),None)
    last_equity=balance
    open_info=None
    if position is not None and end_bar is not None:
        last_equity=equity(end_bar[4])
        mark(end_bar[4])
        counters['open_at_end']=1
        p=position
        open_info={'opened_utc':p['opened'].isoformat(),'units_eur':p['units'],
            'side':p['side'],'stop_bid':p['stop'],'tp_bid':p['tp'],
            'unrealized_usd_if_liquidated_at_end':round(last_equity-balance,2),
            'unclosed_hours':round((end_bar[0]+HOUR-p['opened']).total_seconds()/3600,1)}
    assert sum(counters[k] for k in ('sl','tp','expiry12h','open_at_end'))==counters['entries']
    assert all(t['reason']!='expiry12h' for t in trades) if not time_exit else True
    exit_summary={}
    for reason in ('tp','sl','expiry12h'):
        group=[r for r in trades if r['reason']==reason]
        exit_summary[reason]={'count':len(group),'pnl_usd':round(sum(r['pnl_usd'] for r in group),2),
            'positive':sum(r['pnl_usd']>0 for r in group)}
    return ({'initial_usd':INITIAL_USD,'final_realized_usd':round(balance,2),
        'final_marked_equity_usd':round(last_equity,2),
        'marked_return_pct':round((last_equity/INITIAL_USD-1)*100,2),
        'max_drawdown_pct':round(drawdown*100,2),
        'trades_closed':len(trades),'wins_closed':sum(r['pnl_usd']>0 for r in trades),
        'losses_closed':sum(r['pnl_usd']<0 for r in trades),
        'median_hold_hours':statistics.median(durations) if durations else None,
        'longest_closed_hold_hours':max(durations,default=None),
        'open_at_end':open_info,'events':counters,'exit_breakdown':exit_summary},trades,fills)


def test():
    s=1.10;sp=1.5;sl=.3
    for side in (1,-1):
        entry=execution(s,side,sp,sl)
        stop,tp,risk=levels(side,entry,1.09,1.11,sp,sl)
        assert abs(side*(execution(stop,-side,sp,sl)-entry)+risk)<1e-9
        assert abs(side*(execution(tp,-side,sp,sl)-entry)-3*risk)<1e-9
    start=dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)
    # Construct a 12-hour range episode and ensure a main trade lasts beyond it.
    bars=[]
    for i in range(30):
        t=start+i*HOUR
        if i==0:row=(1.102,1.103,1.101,1.102)
        elif i<12:row=(1.102,1.105,1.101,1.102)
        elif i<21:row=(1.102,1.108,1.101,1.104)
        else:row=(1.104,1.130,1.103,1.12)
        bars.append((t,*row))
    eps=[{'time':start,'lower':1.10,'upper':1.12,'range_atr':True}]
    result,trades,_=backtest(bars,eps,start,start+30*HOUR,'baseline',sp,sl,False)
    assert result['events']['expiry12h']==0
    assert result['events']['entries']>=1
    assert all(row['reason'] in ('sl','tp') for row in trades)
    print('Economic 3R symmetric long/short, no forced expiry, synthetic engine tests PASS')


def main():
    if len(sys.argv)==2 and sys.argv[1]=='--self-test':test();return
    if len(sys.argv)!=2:raise SystemExit('Usage: script data/eurusd_h1.csv')
    path=Path(sys.argv[1]);h1=load_h1(path);h4=load_h4(path)
    _,_,h4atr,_=indicators(h4)
    split=int(len(h4)*.7)
    mid=200+(split-200)//2
    periods=(('train_early',h4[200][0],h4[mid][0]),
             ('validation_late',h4[mid][0],h4[split][0]),
             ('inspected_2023_2026',h4[split][0],h4[-1][0]+dt.timedelta(hours=4)))
    report={'data_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'rules':'Stop at original frozen outer edge. TP bid derived for exactly +3R net versus -1R net after fixed entry/exit spread/slippage; no trailing or hedge; entry eligibility first 12h only; max 3 entries in same eligible UTC day, one live position at a time; indefinitely carry existing position to TP/SL even when flat alert expires; segment-end open valued but NOT force-closed.',
        'entry':'Previously selected reclaim20, compared to original outer30; same original ATR / reference-risk filter, 1.5pct nominal stop risk, 4x gross notional cap and 3pct realized daily loss gate.',
        'data_limits':'BID H1 fixed hypothetical spread/slippage, adverse-first dual-touch, no intrabar order or real ASK, swaps, commission, margin or liquidation; long holds can accrue overnight swap not modeled; TP may lie outside frozen range; chronological validation earlier history only, 2023-26 previously inspected not independent OOS.',
        'periods':{}}
    Path('reports').mkdir(exist_ok=True)
    for name,begin,end in periods:
        ep,audit=collect(h1,h4,h4atr,begin,end)
        section={'audit':audit,'variants':{}}
        for rule in ('reclaim20','baseline'):
            for time_exit in (True,False):
                for cost,(spread,slip) in COSTS.items():
                    key=f'{rule}_{"expiry12h" if time_exit else "hold_until_bracket"}_{cost}'
                    stats,trades,fills=backtest(h1,ep,begin,end,rule,spread,slip,time_exit)
                    section['variants'][key]=stats
                    print(name,key,json.dumps(stats,sort_keys=True))
                    if cost=='base':
                        for suffix,rows in (('trades',trades),('fills',fills)):
                            if rows:
                                with (Path('reports')/f'strategy_a_3r_{name}_{key}_{suffix}.csv').open('w',newline='') as f:
                                    w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
        report['periods'][name]=section
    (Path('reports')/'strategy_a_3r_no_expiry_report.json').write_text(json.dumps(report,indent=2)+'\n')
    print('RESEARCH ONLY; no live broker orders. Open positions are NOT forcibly liquidated.')

if __name__=='__main__':main()
