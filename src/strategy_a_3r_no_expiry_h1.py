"""Research only: Strategy A with fixed 3:1 economic TP/SL and optional expiry.

Frozen prior 80 H1 channel and preselected reclaim20 entry. Entry authorization
lasts 12 hours after midnight; unlimited hold of EXISTING position until TP/SL.
At most one position globally. Matched comparator closes at original 12h
SESSION end (not 12 hours after each entry). No hedging/trailing/live orders.
BID H1, fixed hypothetical spread/slippage, SL-first on ambiguous H1. Swaps,
commissions, margin and broker execution are not modeled. Chronological period
ends censor surviving positions and mark them to market without liquidation.
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

HOUR=dt.timedelta(hours=1)
RISK=.015
CAP=4.


def levels(side, entry, lower, upper, spread, slip):
    stop=lower if side==1 else upper-spread*PIP
    risk=side*(entry-execution(stop,-side,spread,slip))
    if risk<=0:raise ValueError('Stop distance must be positive')
    # Exact net 3R after hypothetical spread and adverse slippage at both fills.
    target_fill=entry+side*3*risk
    target=target_fill+slip*PIP if side==1 else target_fill-(spread+slip)*PIP
    assert abs(side*(execution(target,-side,spread,slip)-entry)-3*risk)<1e-8
    return stop,target,risk


def backtest(h1,episodes,begin,end,rule,spread,slip,timed):
    eligible={e['time']:e for e in episodes if e['range_atr']}
    balance=peak=INITIAL_USD
    drawdown=0.
    p=None
    active=False
    day=None
    limit=None
    day_balance=balance
    day_entries=0
    counts={k:0 for k in ('entries','tp','sl','expiry12h','open_at_end',
        'blocked_midnights','held_over_12h','tp_outside_channel','sl_tp_same_h1',
        'stop_gaps','favorable_tp_gaps','skipped_atr_or_reward','skipped_lot',
        'blocked_session','boundary_pause','atr_pause','resume','days_with_signal')}
    trades=[]
    fills=[]
    durations=[]

    def fill_log(t,event,pos,bid,fill,pnl=0.):
        fills.append({'utc':t.isoformat(),'event':event,'side':pos['side'],
            'bid':round(bid,7),'fill':round(fill,7),'units_eur':pos['units'],
            'pnl_usd':round(pnl,5),'entry_utc':pos['opened'].isoformat()})

    def equity(bid):
        if p is None:return balance
        side=p['side']
        return balance+side*p['units']*(execution(bid,-side,spread,slip)-p['entry'])

    def mark(bid):
        nonlocal drawdown
        value=equity(bid)
        if peak>0:drawdown=max(drawdown,(peak-value)/peak)

    def close(bid,why,when):
        nonlocal p,balance,peak,drawdown
        item=p
        side=item['side']
        fill=execution(bid,-side,spread,slip)
        pnl=side*item['units']*(fill-item['entry'])
        balance+=pnl
        peak=max(peak,balance)
        if peak>0:drawdown=max(drawdown,(peak-balance)/peak)
        hours=(when-item['opened']).total_seconds()/3600
        durations.append(hours)
        if hours>HORIZON:counts['held_over_12h']+=1
        fill_log(when,why,item,bid,fill,pnl)
        trades.append({'opened_utc':item['opened'].isoformat(),
            'closed_utc':when.isoformat(),'side':side,'units_eur':item['units'],
            'entry_fill':round(item['entry'],7),
            'stop_bid':round(item['stop'],7),'tp_bid':round(item['tp'],7),
            'stop_risk_usd':round(item['units']*item['risk'],5),
            'pnl_usd':round(pnl,5),'reason':why,'hold_hours':round(hours,2),
            'tp_outside_frozen_channel':item['outside']})
        counts[why]+=1
        p=None

    for i,(t,op,hi,lo,cl) in enumerate(h1):
        if t<begin:continue
        if t>=end:break
        finish=t+HOUR
        if t.hour==0:
            next_day=eligible.get(t)
            if next_day is not None:
                counts['days_with_signal']+=1
                if p is not None:counts['blocked_midnights']+=1
            day=next_day
            limit=t+HORIZON*HOUR if day else None
            day_balance=balance
            day_entries=0
            active=day is not None
        in_window=(day is not None and day['time']<=t and finish<=limit)
        closed=False
        if p is not None:
            side=p['side']
            hit_sl=(lo<=p['stop'] if side==1 else hi>=p['stop'])
            hit_tp=(hi>=p['tp'] if side==1 else lo<=p['tp'])
            if hit_sl and hit_tp:counts['sl_tp_same_h1']+=1
            if hit_sl:
                price=min(op,p['stop']) if side==1 else max(op,p['stop'])
                if price!=p['stop']:counts['stop_gaps']+=1
                mark(price)
                close(price,'sl',finish)
                closed=True
            elif hit_tp:
                if (op>p['tp'] if side==1 else op<p['tp']):
                    counts['favorable_tp_gaps']+=1
                # TP limit is filled at its limit, never claim gap improvement.
                mark(p['tp'])
                close(p['tp'],'tp',finish)
                closed=True
            else:
                mark(lo if side==1 else hi)
                mark(cl)
                if timed and finish>=p['window_finish']:
                    close(cl,'expiry12h',finish)
                    closed=True

        if p is None and not closed and in_window and active and day_entries<MAX_TRADES_PER_CYCLE and day['lower']<=op<=day['upper']:
            if balance-day_balance<=-.03*day_balance:
                counts['blocked_session']+=1
            elif i>=2:
                prior=h1[max(0,i-80):i]
                history=(range_atr_signal(prior),simple_atr(prior),h1[i-1],h1[i-2])
                side=entry_side(rule,op,day['lower'],day['upper'],history)
                if side:
                    entry=execution(op,side,spread,slip)
                    stop,tp,risk=levels(side,entry,day['lower'],day['upper'],spread,slip)
                    activation=day['lower']+(.52 if side==1 else .48)*(day['upper']-day['lower'])
                    reward=side*(execution(activation,-side,spread,slip)-entry)
                    if history[1] is None or risk<history[1] or reward<=0:
                        counts['skipped_atr_or_reward']+=1
                    else:
                        by_risk=math.floor(balance*RISK/risk/LOT_UNITS)*LOT_UNITS
                        by_cap=math.floor(balance*CAP/entry/LOT_UNITS)*LOT_UNITS
                        units=int(min(by_risk,by_cap))
                        if units<LOT_UNITS:
                            counts['skipped_lot']+=1
                        else:
                            outside=tp>day['upper'] if side==1 else tp<day['lower']
                            p={'side':side,'entry':entry,'opened':t,
                                'units':units,'stop':stop,'tp':tp,'risk':risk,
                                'outside':outside,'window_finish':limit}
                            counts['entries']+=1
                            day_entries+=1
                            if outside:counts['tp_outside_channel']+=1
                            fill_log(t,'open',p,op,entry)
                            # As in the original engine, entry at known H1 OPEN
                            # precedes exits in this same H1; adverse event first.
                            hs=(lo<=stop if side==1 else hi>=stop)
                            ht=(hi>=tp if side==1 else lo<=tp)
                            if hs and ht:counts['sl_tp_same_h1']+=1
                            if hs:
                                price=min(op,stop) if side==1 else max(op,stop)
                                if price!=stop:counts['stop_gaps']+=1
                                mark(price)
                                close(price,'sl',finish)
                                closed=True
                            elif ht:
                                mark(tp)
                                close(tp,'tp',finish)
                                closed=True
                            else:
                                mark(lo if side==1 else hi)
                                mark(cl)
                            # If entry bar is final bar of 12h window, the
                            # matched comparator must close it at window end.
                            if timed and p is not None and finish>=limit:
                                close(cl,'expiry12h',finish)
                                closed=True
        if in_window:
            outside=lo<day['lower'] or hi>day['upper']
            if outside:
                if active:counts['boundary_pause']+=1
                active=False
            else:
                ratio=range_atr_signal(h1[max(0,i-79):i+1])
                if active and (ratio is None or ratio>=5):
                    counts['atr_pause']+=1
                    active=False
                elif not active and ratio is not None and ratio<5 and finish<limit:
                    counts['resume']+=1
                    active=True

    last=next((x for x in reversed(h1) if begin<=x[0]<end),None)
    final_equity=balance
    unresolved=None
    if p is not None and last is not None:
        final_equity=equity(last[4])
        mark(last[4])
        counts['open_at_end']=1
        unresolved={'opened_utc':p['opened'].isoformat(),'side':p['side'],
            'units_eur':p['units'],'stop_bid':p['stop'],'tp_bid':p['tp'],
            'unrealized_usd_if_liquidated':round(final_equity-balance,2),
            'hours_open':round((last[0]+HOUR-p['opened']).total_seconds()/3600,2)}
    assert sum(counts[x] for x in ('sl','tp','expiry12h','open_at_end'))==counts['entries']
    if not timed:assert counts['expiry12h']==0
    breakdown={}
    for why in ('tp','sl','expiry12h'):
        matches=[x for x in trades if x['reason']==why]
        breakdown[why]={'count':len(matches),
            'pnl_usd':round(sum(x['pnl_usd'] for x in matches),2),
            'wins':sum(x['pnl_usd']>0 for x in matches)}
    result={'initial_usd':INITIAL_USD,'final_realized_usd':round(balance,2),
        'final_marked_equity_usd':round(final_equity,2),
        'marked_return_pct':round(100*(final_equity/INITIAL_USD-1),2),
        'max_drawdown_pct':round(drawdown*100,2),
        'trades_closed':len(trades),'wins_closed':sum(x['pnl_usd']>0 for x in trades),
        'losses_closed':sum(x['pnl_usd']<0 for x in trades),
        'median_hold_hours':statistics.median(durations) if durations else None,
        'longest_closed_hold_hours':max(durations,default=None),
        'open_at_end':unresolved,'events':counts,'exit_breakdown':breakdown}
    return result,trades,fills


def test():
    spread,slip=1.5,.3
    for side in (1,-1):
        entry=execution(1.102,side,spread,slip)
        stop,tp,risk=levels(side,entry,1.10,1.12,spread,slip)
        assert abs(side*(execution(stop,-side,spread,slip)-entry)+risk)<1e-9
        assert abs(side*(execution(tp,-side,spread,slip)-entry)-3*risk)<1e-9
    start=dt.datetime(2026,1,5,tzinfo=dt.timezone.utc)
    h1=[]
    # All indicators have 80 real PRIOR H1 before midnight. TP comes 30h later.
    for j in range(-80,40):
        t=start+j*HOUR
        row=(1.102,1.103,1.101,1.102) if j<30 else (1.102,1.115,1.101,1.113)
        h1.append((t,*row))
    episodes=[{'time':start,'lower':1.10,'upper':1.12,'range_atr':True}]
    hold,hold_trades,_=backtest(h1,episodes,start,start+40*HOUR,'baseline',spread,slip,False)
    timed,timed_trades,_=backtest(h1,episodes,start,start+40*HOUR,'baseline',spread,slip,True)
    assert hold['events']['entries']==1 and hold['events']['tp']==1,hold
    assert hold['events']['held_over_12h']==1 and hold['events']['expiry12h']==0,hold
    assert timed['events']['entries']==1 and timed['events']['expiry12h']==1,timed
    assert hold_trades[0]['hold_hours']>12 and timed_trades[0]['hold_hours']==12
    print('Economic exact 3R, >12h carry, session-end comparator, no hedge: PASS')


def main():
    if len(sys.argv)==2 and sys.argv[1]=='--self-test':test();return
    if len(sys.argv)!=2:raise SystemExit('Usage: script data/eurusd_h1.csv')
    path=Path(sys.argv[1]);h1=load_h1(path);h4=load_h4(path)
    _,_,atr,_=indicators(h4)
    split=int(len(h4)*.7)
    mid=200+(split-200)//2
    spans=(('train_early',h4[200][0],h4[mid][0]),
           ('validation_late',h4[mid][0],h4[split][0]),
           ('inspected_2023_2026',h4[split][0],h4[-1][0]+dt.timedelta(hours=4)))
    report={'data_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'rules':'3R net target versus 1R boundary stop; entry opens only within qualifying 12h window; existing position remains until stop/TP regardless of regime. Comparator closes at original 12h window END. One live position globally; no hedges or trailing; unresolved final positions marked but not liquidated.',
        'sizing':'Original 1.5pct reference stop risk, max 4x cash notional, 1000 EUR min lot, max 3 per 12h window and 3pct realized session loss gate.',
        'limitations':'H1 BID OHLC, fixed hypothetical spreads/slippage, adverse-first if TP+SL same H1; no swaps, commissions, intrabar sequencing, real ASK, broker margin/liquidation. Targets may exceed frozen channel. 2023-26 repeatedly inspected, not fresh holdout.',
        'periods':{}}
    Path('reports').mkdir(exist_ok=True)
    for name,begin,end in spans:
        episodes,audit=collect(h1,h4,atr,begin,end)
        block={'audit':audit,'variants':{}}
        for rule in ('reclaim20','baseline'):
            for timed in (True,False):
                for cost,(spread,slip) in COSTS.items():
                    key=f'{rule}_{"expiry12h" if timed else "hold_until_bracket"}_{cost}'
                    result,trades,fills=backtest(h1,episodes,begin,end,rule,spread,slip,timed)
                    block['variants'][key]=result
                    print(name,key,json.dumps(result,sort_keys=True))
                    if cost=='base':
                        for suffix,rows in (('trades',trades),('fills',fills)):
                            if rows:
                                dest=Path('reports')/f'strategy_a_3r_{name}_{key}_{suffix}.csv'
                                with dest.open('w',newline='') as f:
                                    w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
        report['periods'][name]=block
    (Path('reports')/'strategy_a_3r_no_expiry_report.json').write_text(json.dumps(report,indent=2)+'\n')
    print('RESEARCH ONLY. No live orders; unresolved positions never force-closed.')

if __name__=='__main__':main()
