"""RESEARCH ONLY. No orders, brokers or deployment.

52% of fixed channel activates a protected exit at 52%; once activated,
trailing stop rises/falls with COMPLETED H1 closes by 2% CHANNEL WIDTH.
Execution starts next bar; no intrabar lookahead or promised same-bar fills.
At boundary, retain main and open opposite hedge at 2x USD NOTIONAL measured
at each leg's own entry, not 2x EUR units or 2x margin. Hedge has separate
break-even stop after spread/slippage, without cancelling main. Both legs
close at 12h expiry. Same-candle hedge return/rebreach deferred conservatively.
BID-only H1, hypothetical fixed costs, no margin feasibility, swap, real ASK,
broker commission, intrabar chronology or fresh out-of-sample. No live trades.
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
MAIN_NOTIONAL_CAP = 4.0
ACTIVATE = .52
TRAIL_WIDTH = .02


def usd_hedge_units(main_units, main_entry, hedge_entry, multiple):
    if min(main_units, main_entry, hedge_entry, multiple) <= 0:
        raise ValueError('Notional and prices must be positive')
    return multiple * main_units * main_entry / hedge_entry


def hedge_break_even_bid(hedge_side, hedge_entry, spread, slip):
    """Bid threshold where opposite fill exactly offsets hedge entry cost."""
    if hedge_side == -1:  # short hedge: buy at ASK to close
        return hedge_entry - (spread + slip) * PIP
    if hedge_side == 1:  # long hedge: sell at BID to close
        return hedge_entry + slip * PIP
    raise ValueError('Wrong side')


def cycle(bars, ratios, lower, upper, balance, peak, drawdown, multiple, trailing, spread, slip):
    if len(bars) != HORIZON or len(ratios) != HORIZON or not lower < upper:
        raise ValueError('Invalid bars or channel')
    width = upper-lower
    active = True
    p = None
    entries = 0
    beginning = balance
    ev = {k:0 for k in ('entries','trailing_armed','trailing_exits','fixed52_exits',
        'main_expiry','hedges_opened','hedges_breakeven_stops','hedges_expired',
        'rehedges','ambiguous_hedge_return_rebreach','boundary_pauses','atr_pauses',
        'resumes','skipped','skipped_size','session_block','gap_beyond_boundary')}
    actions, finished = [], []

    def log(when, kind, side, bid, fill, units, pnl=0):
        actions.append({'time':when.isoformat(),'event':kind,
                        'side':'long' if side == 1 else 'short',
                        'bid':round(bid,7),'fill':round(fill,7),
                        'units_eur':round(units,5),'realized_usd':round(pnl,5)})

    def equity(price):
        if p is None:
            return balance
        side, u = p['side'], p['units']
        x = balance + side*u*(execution(price,-side,spread,slip)-p['entry'])
        if p['hedged']:
            x -= side*p['hedge_units']*(execution(price,side,spread,slip)-p['hedge_entry'])
        return x

    def mark(price):
        nonlocal peak,drawdown
        e = equity(price)
        peak = max(peak,e)
        if peak > 0:
            drawdown = max(drawdown,(peak-e)/peak)

    def close_main(price, why, when):
        nonlocal p,balance
        s,u = p['side'],p['units']
        fill = execution(price,-s,spread,slip)
        pnl = s*u*(fill-p['entry'])
        balance += pnl
        p['pnl'] += pnl
        log(when,why,-s,price,fill,u,pnl)
        finished.append({'opened':p['opened'].isoformat(),
            'closed':when.isoformat(),'reason':why,
            'total_pnl_usd':round(p['pnl'],5),
            'hedge_count':p['hedge_count'],
            'hedged_at_expiry':p['hedged_at_expiry']})
        ev[why] += 1
        p = None
        mark(price)

    for j,(t,op,hi,lo,cl) in enumerate(bars):
        when = t+dt.timedelta(hours=1)
        released = False

        # A hedge stop becomes eligible only the NEXT H1 after hedge fill.
        if p is not None and p['hedged'] and j > p['hedge_hour']:
            hedge_side = -p['side']
            be = hedge_break_even_bid(hedge_side,p['hedge_entry'],spread,slip)
            prior = bars[j-1][4]
            direction_ready = prior < be if hedge_side == -1 else prior > be
            touched = hi >= be if hedge_side == -1 else lo <= be
            contradictory = lo <= lower if p['side'] == 1 else hi >= upper
            if direction_ready and touched:
                if contradictory:
                    # Both chronological paths possible; cannot award a free
                    # BE hedge stop followed by another intrabar hedge.
                    ev['ambiguous_hedge_return_rebreach'] += 1
                else:
                    bid = (max(op,be) if hedge_side == -1 else min(op,be))
                    fill = execution(bid,-hedge_side,spread,slip)
                    hpnl = hedge_side*p['hedge_units']*(fill-p['hedge_entry'])
                    balance += hpnl
                    p['pnl'] += hpnl
                    log(t,'stop_hedge_at_breakeven',-hedge_side,bid,fill,p['hedge_units'],hpnl)
                    p['hedged'] = False
                    ev['hedges_breakeven_stops'] += 1
                    released = True
                    mark(bid)

        if p is None and active and lower <= op <= upper and entries < MAX_TRADES_PER_CYCLE:
            if balance-beginning <= -.03*beginning:
                ev['session_block'] += 1
            else:
                side = (1 if op <= lower+ENTRY_FRACTION*width else
                        -1 if op >= upper-ENTRY_FRACTION*width else 0)
                if side:
                    entry = execution(op,side,spread,slip)
                    boundary = lower if side == 1 else upper-spread*PIP
                    exit_ref = execution(boundary,-side,spread,slip)
                    reference_loss = side*(entry-exit_ref)
                    trigger = lower+(ACTIVATE if side == 1 else 1-ACTIVATE)*width
                    net_reward = side*(execution(trigger,-side,spread,slip)-entry)
                    atr = ratios[j][1]
                    if atr is None or reference_loss <= 0 or reference_loss < atr or net_reward <= 0:
                        ev['skipped'] += 1
                    else:
                        by_ref = math.floor(balance*RISK_REFERENCE/reference_loss/LOT_UNITS)*LOT_UNITS
                        by_cap = math.floor(balance*MAIN_NOTIONAL_CAP/entry/LOT_UNITS)*LOT_UNITS
                        units = int(min(by_ref,by_cap))
                        if units < LOT_UNITS:
                            ev['skipped_size'] += 1
                        else:
                            p = {'opened':t,'side':side,'units':units,'entry':entry,
                                 'hedged':False,'hedge_units':0.,'hedge_entry':0.,
                                 'hedge_hour':-1,'hedge_count':0,'pnl':0.,
                                 'trailing':False,'trail_stop':None,
                                 'hedged_at_expiry':False}
                            entries += 1
                            ev['entries'] += 1
                            log(t,'open_main',side,op,entry,units)
                            mark(op)

        if p is not None and not p['hedged'] and not released:
            s = p['side']
            boundary = lower if s == 1 else upper-spread*PIP
            trigger = lower+(ACTIVATE if s == 1 else 1-ACTIVATE)*width
            stop = p['trail_stop']
            if p['trailing'] and ((lo <= stop) if s == 1 else (hi >= stop)):
                # Stop already existed before bar began; gap fill can be worse.
                price = min(op,stop) if s == 1 else max(op,stop)
                mark(price)
                close_main(price,'trailing_exits' if trailing else 'fixed52_exits',when)
            elif (lo <= boundary if s == 1 else hi >= boundary):
                bid = min(op,boundary) if s == 1 else max(op,boundary)
                if (op < boundary if s == 1 else op > boundary):
                    ev['gap_beyond_boundary'] += 1
                mark(bid)
                hedge_entry = execution(bid,-s,spread,slip)
                hu = usd_hedge_units(p['units'],p['entry'],hedge_entry,multiple)
                if p['hedge_count']:
                    ev['rehedges'] += 1
                p['hedged'] = True
                p['hedge_entry'] = hedge_entry
                p['hedge_units'] = hu
                p['hedge_hour'] = j
                p['hedge_count'] += 1
                ev['hedges_opened'] += 1
                log(when,'open_opposite_hedge',-s,bid,hedge_entry,hu)
                mark(bid)
            elif not trailing and ((hi >= trigger) if s == 1 else (lo <= trigger)):
                # Fixed comparator can fill at known trigger, except gap.
                bid = max(op,trigger) if s == 1 else min(op,trigger)
                mark(bid)
                close_main(bid,'fixed52_exits',when)
            else:
                mark(lo if s == 1 else hi)
                # Trailing activation and updates based only on COMPLETED close.
                # It cannot retroactively fill or exploit H1 high/low ordering.
                if trailing and ((cl >= trigger) if s == 1 else (cl <= trigger)):
                    if not p['trailing']:
                        p['trailing'] = True
                        ev['trailing_armed'] += 1
                    candidate = max(trigger,cl-TRAIL_WIDTH*width) if s == 1 else min(trigger,cl+TRAIL_WIDTH*width)
                    p['trail_stop'] = (max(p['trail_stop'],candidate) if s == 1 else
                                       min(p['trail_stop'],candidate)) if p['trail_stop'] is not None else candidate

        if p is not None:
            # Both extremes are checked, including over-hedge reversal risk.
            mark(lo)
            mark(hi)
            mark(cl)
        breach = lo < lower or hi > upper
        if breach:
            if active:
                ev['boundary_pauses'] += 1
            active = False
        elif active:
            if ratios[j][0] is None or ratios[j][0] >= 5:
                ev['atr_pauses'] += 1
                active = False
        elif ratios[j][0] is not None and ratios[j][0] < 5 and j < HORIZON-1:
            ev['resumes'] += 1
            active = True

    if p is not None:
        when = bars[-1][0]+dt.timedelta(hours=1)
        bid = bars[-1][4]
        if p['hedged']:
            p['hedged_at_expiry'] = True
            hedge_side = -p['side']
            fill = execution(bid,-hedge_side,spread,slip)
            hpnl = hedge_side*p['hedge_units']*(fill-p['hedge_entry'])
            balance += hpnl
            p['pnl'] += hpnl
            log(when,'expire_hedge',-hedge_side,bid,fill,p['hedge_units'],hpnl)
            ev['hedges_expired'] += 1
            p['hedged'] = False
        close_main(bid,'main_expiry',when)
    assert p is None and entries <= MAX_TRADES_PER_CYCLE
    return balance,peak,drawdown,ev,actions,finished


def run(h1, episodes, multiple, trailing, spread, slip):
    by_time = {bar[0]:i for i,bar in enumerate(h1)}
    balance = peak = INITIAL_USD
    dd = 0.
    counts = {}
    positions, actions = [], []
    for ep in episodes:
        if not ep['range_atr']:
            continue
        i = by_time[ep['time']]
        ratios = [(range_atr_signal(h1[i-80:i+j+1]),simple_atr(h1[i-80:i+j]))
                  for j in range(HORIZON)]
        balance,peak,dd,ev,ledger,closed = cycle(h1[i:i+HORIZON],ratios,
            ep['lower'],ep['upper'],balance,peak,dd,multiple,trailing,spread,slip)
        for key,value in ev.items():
            counts[key] = counts.get(key,0)+value
        actions += [{'cycle':ep['time'].isoformat(),**r} for r in ledger]
        positions += [{'cycle':ep['time'].isoformat(),**r} for r in closed]
    wins = sum(x['total_pnl_usd']>0 for x in positions)
    losses = sum(x['total_pnl_usd']<0 for x in positions)
    breakdown = {}
    for label,subset in [('target_or_trailing',[x for x in positions if x['reason'] != 'main_expiry']),
                         ('expiry_ever_hedged',[x for x in positions if x['reason'] == 'main_expiry' and x['hedge_count']]),
                         ('expiry_never_hedged',[x for x in positions if x['reason'] == 'main_expiry' and not x['hedge_count']])]:
        breakdown[label] = {'n':len(subset),'pnl_usd':round(sum(x['total_pnl_usd'] for x in subset),2),
                            'wins':sum(x['total_pnl_usd']>0 for x in subset),
                            'losses':sum(x['total_pnl_usd']<0 for x in subset)}
    return {'final_usd':round(balance,2),'return_pct':round((balance/INITIAL_USD-1)*100,2),
            'max_worst_hour_drawdown_pct':round(dd*100,2),'positions':len(positions),
            'wins':wins,'losses':losses,'win_rate_pct':round(100*wins/len(positions),2) if positions else 0,
            'breakdown':breakdown,'events':counts},positions,actions


def self_test():
    units = usd_hedge_units(1000,1.1,1.09,2.)
    assert abs(units*1.09 - 2*1000*1.1) < 1e-8
    spread,slip = 1.5,.3
    for hedge_side,entry in [(-1,1.08997),(1,1.09018)]:
        be = hedge_break_even_bid(hedge_side,entry,spread,slip)
        assert abs(hedge_side*(execution(be,-hedge_side,spread,slip)-entry)) < 1e-10
    assert abs((max(1.52,1.56-.02)-1.54)) < 1e-10
    print('USD 1:2 notional, hedge break-even fill and trail math tests PASS')


def main():
    if len(sys.argv)==2 and sys.argv[1]=='--self-test':
        self_test()
        return
    if len(sys.argv)!=2:
        raise SystemExit('Usage: python src/hedge_2x_trailing52_h1_research.py data/eurusd_h1.csv')
    path = Path(sys.argv[1]);h1 = load_h1(path);h4 = load_h4(path)
    _,_,h4atr,_ = indicators(h4)
    split = int(len(h4)*.7)
    ranges = [('development',h4[200][0],h4[split][0]),
              ('previously_inspected_2023_2026',h4[split][0],h4[-1][0]+dt.timedelta(hours=4))]
    report = {'data_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'terms':'52 percent of frozen range is floor/activation; 2 percent of channel width is trailing gap, not 2 percent of FX price; close-based trailing activates next H1',
        'hedge':'2x USD notional of original main at separate original fill prices; break-even stop net fixed fill costs; no main stop; 12h forced closure both',
        'risk':'1.5pct reference sizing against original boundary only, NOT guaranteed loss bound; initial main 4x notional/equity cap; hedged pair can reach about 12x gross; margin not modeled',
        'limitations':'Research only, no orders or broker; BID H1 no tick/ASK or intrabar chronology; hedge same-bar return not executed, ambiguous return/rebreach deferred; hypothetical spread/slippage base and double, no broker commission, swap, variable spread, margin, liquidation; repeatedly inspected post-2023 sample NOT fresh holdout.',
        'segments':{}}
    Path('reports').mkdir(exist_ok=True)
    for name,begin,end in ranges:
        episodes,audit = collect(h1,h4,h4atr,begin,end)
        output = {'audit':audit,'variants':{}}
        for multiple,trailing in [(1.,False),(2.,False),(1.,True),(2.,True)]:
            for cost,(spread,slip) in COSTS.items():
                key = f'hedge{int(multiple)}x_{"trailing52_2" if trailing else "fixed52"}_{cost}'
                metrics,positions,actions = run(h1,episodes,multiple,trailing,spread,slip)
                output['variants'][key] = metrics
                print(name,key,json.dumps(metrics,sort_keys=True))
                if cost == 'base':
                    for suffix,rows in [('positions',positions),('actions',actions)]:
                        with (Path('reports')/f'hedge2_trail_{name}_{key}_{suffix}.csv').open('w',newline='') as f:
                            if rows:
                                writer = csv.DictWriter(f,fieldnames=list(rows[0]))
                                writer.writeheader()
                                writer.writerows(rows)
        report['segments'][name] = output
    (Path('reports')/'hedge2_trailing52_report.json').write_text(json.dumps(report,indent=2)+'\n')
    print('RESEARCH ONLY: no brokerage trading or guarantees.')


if __name__ == '__main__':
    main()
