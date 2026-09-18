"""Offline entry-only comparison for Strategy A. NO orders or broker access.

Reuse the baseline 12-hour simulator EXACTLY, substituting only its entry-side
expression for a prior-H1-confirmed entry rule. Verify full-ledger baseline
parity before optimization. Frozen prior-80-H1 bounds, same ATR filter, sizing,
stop, 52% confirmed trailing, 2% channel-width gap, and 12h expiry.

Select by chronological EARLY development return only; show untouched-by-this-
search later development validation separately. 2023-26 is PREVIOUSLY INSPECTED
and must NOT be represented as a genuine fresh holdout. BID H1 and fixed
hypothetical fees cannot establish live profitability or same-hour order.
"""
import csv
import datetime as dt
import hashlib
import inspect
import json
import sys
from pathlib import Path

import strategy_a_no_hedge_trailing_h1 as original
from aggressive_range_backtest_h1 import COSTS, INITIAL_USD
from compare_tpo_atr_h1 import HORIZON, collect, range_atr_signal, simple_atr
from range_guard_h1 import load_h1
from trend_h4 import indicators, load_h4

RULES = ('baseline', 'deep20', 'deep15', 'reversal30', 'reversal20',
         'reclaim20', 'two_closes')
REPLACE = ('side=1 if op <= lower+ENTRY_FRACTION*width else -1 if '
           'op >= upper-ENTRY_FRACTION*width else 0')


def entry_side(rule, op, lower, upper, historic):
    width = upper - lower
    if not width > 0 or not lower <= op <= upper:
        return 0
    prev, older = historic[2:4]
    for side in (1, -1):
        distance = ((op-lower) if side == 1 else (upper-op))/width
        if rule in ('baseline', 'deep20', 'deep15'):
            band = {'baseline': .30, 'deep20': .20, 'deep15': .15}[rule]
            if distance <= band:
                return side
            continue
        _, po, ph, pl, pc = prev
        _, oo, oh, ol, oc = older
        candle_width = ph-pl
        if candle_width <= 0:
            continue
        wick_distance = ((pl-lower) if side == 1 else (upper-ph))/width
        close_distance = ((pc-lower) if side == 1 else (upper-pc))/width
        close_fraction = ((pc-pl) if side == 1 else (ph-pc))/candle_width
        bullish_reversal = side*(pc-po) > 0
        if rule == 'reversal30':
            accepted = (distance <= .30 and 0 <= wick_distance <= .30
                        and 0 <= close_distance <= .40
                        and bullish_reversal and close_fraction >= .60)
        elif rule == 'reversal20':
            accepted = (distance <= .25 and 0 <= wick_distance <= .15
                        and 0 <= close_distance <= .35
                        and bullish_reversal and close_fraction >= .65)
        elif rule == 'reclaim20':
            accepted = (distance <= .40 and 0 <= wick_distance <= .15
                        and .20 <= close_distance <= .45
                        and bullish_reversal)
        elif rule == 'two_closes':
            older_wick = ((ol-lower) if side == 1 else (upper-oh))/width
            accepted = (distance <= .35 and 0 <= wick_distance <= .25
                        and 0 <= older_wick <= .30 and close_distance <= .40
                        and side*(pc-oc) > 0 and bullish_reversal)
        else:
            raise ValueError(rule)
        if accepted:
            return side
    return 0


def simulator_for(rule):
    """Keep existing exit/sizing engine byte-identical; replace ONE entry line."""
    source = inspect.getsource(original.simulate_cycle)
    assert source.count(REPLACE) == 1, 'Original engine changed: audit required'
    source = source.replace(REPLACE, 'side=choose_entry(op,lower,upper,historic[j])')
    scope = vars(original).copy()
    scope['choose_entry'] = lambda op, lo, hi, hist: entry_side(rule,op,lo,hi,hist)
    exec(compile(source, 'strategy_a_baseline_entry_only', 'exec'), scope)
    return scope['simulate_cycle']


def get_contexts(h1, episodes):
    by_time = {b[0]:i for i,b in enumerate(h1)}
    result=[]
    for ep in episodes:
        if not ep['range_atr']:
            continue
        i=by_time[ep['time']]
        hist=[]
        for j in range(HORIZON):
            idx=i+j
            hist.append((range_atr_signal(h1[i-80:idx+1]),
                         simple_atr(h1[i-80:idx]),h1[idx-1],h1[idx-2]))
        result.append((ep,h1[i:i+HORIZON],hist))
    return result


def evaluate(contexts, engine, spread, slip, ledgers=False):
    balance=peak=INITIAL_USD
    dd=0.
    count={}
    trades=[]
    fills=[]
    for ep,bars,hist in contexts:
        balance,peak,dd,event,t,f=engine(bars,hist,ep['lower'],ep['upper'],
                                         balance,peak,dd,spread,slip,True)
        for k,v in event.items():
            count[k]=count.get(k,0)+v
        trades.extend(({'cycle_utc':ep['time'].isoformat(),**row} for row in t))
        if ledgers:
            fills.extend(({'cycle_utc':ep['time'].isoformat(),**row} for row in f))
    stats={}
    for reason in ('stops','trailing_exits','expiries'):
        group=[x for x in trades if x['reason']==reason]
        stats[reason]={'n':len(group),'net_usd':round(sum(x['pnl_usd'] for x in group),2),
                       'wins':sum(x['pnl_usd']>0 for x in group),
                       'losses':sum(x['pnl_usd']<0 for x in group)}
    return {'final_usd':round(balance,2),'return_pct':round(100*(balance/INITIAL_USD-1),2),
            'drawdown_pct':round(dd*100,2),'trades':len(trades),
            'wins':sum(x['pnl_usd']>0 for x in trades),
            'losses':sum(x['pnl_usd']<0 for x in trades),
            'breakdown':stats,'events':count},trades,fills


def tests():
    start=dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)
    p=(start,1.15,1.24,1.09,1.22)
    older=(start-dt.timedelta(hours=1),1.13,1.20,1.10,1.12)
    h=(4.,.01,p,older)
    assert entry_side('baseline',1.2,1.,2.,h)==1
    assert entry_side('deep15',1.2,1.,2.,h)==0
    assert entry_side('reversal30',1.20,1.,2.,h)==1
    assert entry_side('reversal30',1.80,1.,2.,h)==0
    assert simulator_for('baseline') and simulator_for('reversal30')
    original.self_test()
    print('Entry symmetry, no-lookahead dependencies and original engine self-tests PASS')


def write_csv(dest,rows):
    if not rows:
        return
    with dest.open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)


def main():
    if len(sys.argv)==2 and sys.argv[1]=='--self-test':
        tests();return
    if len(sys.argv)!=2:
        raise SystemExit('Usage: python src/strategy_a_entry_optimization_h1.py data/eurusd_h1.csv')
    path=Path(sys.argv[1]);h1=load_h1(path);h4=load_h4(path)
    _,_,atr,_=indicators(h4)
    split=int(len(h4)*.7)
    midpoint=200+(split-200)//2
    spans=(('train_early_development',h4[200][0],h4[midpoint][0]),
           ('validation_late_development',h4[midpoint][0],h4[split][0]),
           ('previously_inspected_2023_2026',h4[split][0],h4[-1][0]+dt.timedelta(hours=4)))
    contexts={};audits={}
    for name,a,b in spans:
        eps,audit=collect(h1,h4,atr,a,b)
        contexts[name]=get_contexts(h1,eps)
        audits[name]=audit
    engines={r:simulator_for(r) for r in RULES}
    base_spread,base_slip=COSTS['base']
    original_res,original_trades,original_fills=evaluate(
        contexts['train_early_development'], original.simulate_cycle,
        base_spread,base_slip,True)
    patched_res,patched_trades,patched_fills=evaluate(
        contexts['train_early_development'], engines['baseline'],
        base_spread,base_slip,True)
    assert (original_res,original_trades,original_fills)==(patched_res,patched_trades,patched_fills), 'Baseline parity FAILED'
    print('FULL BASELINE RESULT + EVERY TRADE AND FILL PARITY: PASS')
    report={'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'purpose':'Entry ONLY selection with baseline-identical fills/exits, no hedge',
            'rule_definitions':{
                'baseline':'Enter at open in outer 30pct, no turn confirmation',
                'deep20':'Enter at open in outer 20pct, no confirmation',
                'deep15':'Enter at open in outer 15pct, no confirmation',
                'reversal30':'Last CLOSED H1 directional body, close top/bottom 40pct of candle; prior wick near outer 30pct; current open outer 30pct',
                'reversal20':'Stricter prior H1 directional close top/bottom 35pct, wick outer 15pct, open outer 25pct',
                'reclaim20':'Prior wick outer 15pct, directional close recovered to 20-45pct, next open outer 40pct',
                'two_closes':'Two consecutive improving closes after near-edge wicks, last candle directional, open outer 35pct'},
            'selection':'Highest train_early_development compounded return, minimum 30 training trades; ties deterministic alphabetical; NO access to validation during selection',
            'periods':{},
            'limits':'2023-26 already repeatedly inspected, not independent OOS; validation is chronological but still historical exploratory. H1 BID, hypothetical static 1.5 pip spread/0.3 pip slippage per fill, no swap, commission, real ASK or broker fill; stops adverse-first and arming only on completed H1. No actual orders.'}
    all_results={}
    for name,_,_ in spans:
        entries={}
        for rule in RULES:
            metrics,_,_=evaluate(contexts[name],engines[rule],base_spread,base_slip)
            entries[rule]=metrics
            print(name,rule,json.dumps({'return_pct':metrics['return_pct'],
                      'trades':metrics['trades'],'wins':metrics['wins'],
                      'stops':metrics['breakdown']['stops']['n'],
                      'trail':metrics['breakdown']['trailing_exits']['n'],
                      'expiry':metrics['breakdown']['expiries']['n']},sort_keys=True))
        report['periods'][name]={'audit':audits[name], 'flat_opportunities':len(contexts[name]),'variants':entries}
        all_results[name]=entries
    candidates=[r for r in RULES if all_results['train_early_development'][r]['trades']>=30]
    winner=sorted(candidates,key=lambda r:(-all_results['train_early_development'][r]['return_pct'],r))[0]
    report['training_selected_rule']=winner
    report['baseline_parity_verified']=True
    print('TRAIN ONLY SELECTED RULE:',winner)
    Path('reports').mkdir(exist_ok=True)
    for name,_,_ in spans:
        for r in ('baseline',winner):
            metric,trade,fill=evaluate(contexts[name],engines[r],base_spread,base_slip,True)
            write_csv(Path('reports')/f'strategy_a_entry_{name}_{r}_trades.csv',trade)
            write_csv(Path('reports')/f'strategy_a_entry_{name}_{r}_fills.csv',fill)
        stress={}
        for r in ('baseline',winner):
            metrics,_,_=evaluate(contexts[name],engines[r],*COSTS['double'])
            stress[r]=metrics
        report['periods'][name]['double_costs']=stress
        print(name,'selected',winner,'base',all_results[name][winner]['return_pct'],
              'baseline',all_results[name]['baseline']['return_pct'],
              'selected_double',stress[winner]['return_pct'],
              'baseline_double',stress['baseline']['return_pct'])
    (Path('reports')/'strategy_a_entry_optimization_report.json').write_text(json.dumps(report,indent=2)+'\n')
    print('RESEARCH ONLY. Strategy A original unchanged. NO live orders.')

if __name__=='__main__':main()
