"""Research-only simulation of a 12h ATR range cycle with pause and resume.

A signal occurs at 00:00 UTC using prior closed H1 candles only. Freeze the
preceding 80h extremes and follow 12 contiguous H1 bars. A wick outside the
bounds triggers a *hypothetical* boundary pause. Because H1 OHLC cannot reveal
its intrabar crossing time, the ENTIRE breaching hour is NOT counted as safe
trading time. After a paused cycle, require a COMPLETED fully-contained hour
and a fresh Range/ATR < 5 computed at its close; rearm for the NEXT hour only.
Also suspend at the NEXT H1 open if a completed bar invalidates the ATR signal.
Never assume an intra-hour return or a favorable OHLC ordering. The cycle
expires after exactly 12 hours; one new opportunity is sampled at next UTC
midnight (not continuously auto-rearmed). No positions, fills, PnL or orders.
"""
import csv
import datetime as dt
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path

from compare_tpo_atr_h1 import HORIZON, HOUR, collect, future_oracle, pct, range_atr_signal
from range_guard_h1 import load_h1
from trend_h4 import indicators, load_h4

THRESHOLD = 5.0


def simulate_cycle(window, lower, upper, ratios):
    """Returns session totals and an auditable hour-by-hour event ledger.

    Ratios[j] must be known only at hour j CLOSE. For boundary-crossing bars,
    no within-hour safe duration can be inferred; report zero eligible hours.
    """
    if (len(window) != HORIZON or len(ratios) != HORIZON or
            not math.isfinite(lower) or not math.isfinite(upper) or lower >= upper or
            any(window[j][0] - window[j-1][0] != HOUR for j in range(1, HORIZON))):
        raise ValueError('Expected 12 contiguous hours and valid frozen bounds')
    active = True
    prior_breach = False
    any_breach = False
    fully_active = breach_hours = inside_paused = excursions = 0
    boundary_pauses = ratio_pauses = resumes = 0
    rearmed_after_breach = False
    first_breach_hour = None
    max_wick_overshoot = 0.0
    hours = []
    for j, (timestamp, op, high, low, close) in enumerate(window):
        breach = high > upper or low < lower
        ratio = ratios[j]
        ratio_ok = ratio is not None and math.isfinite(ratio) and ratio < THRESHOLD
        safe = active and not breach
        rearm = False
        paused_by_ratio = False
        if safe:
            fully_active += 1
        elif breach:
            breach_hours += 1
        else:
            inside_paused += 1
        if breach:
            max_wick_overshoot = max(max_wick_overshoot, high-upper, lower-low, 0.0)
            if not prior_breach:
                excursions += 1
            if not any_breach:
                first_breach_hour = j
            any_breach = True
            if active:
                boundary_pauses += 1
            active = False
        elif active:
            # Only observable at this H1 close; affects NEXT hour.
            if not ratio_ok:
                ratio_pauses += 1
                paused_by_ratio = True
                active = False
        else:
            # Neither same-hour wick re-entry nor same-hour ratio may create
            # a fully eligible hour; the following hour can resume.
            if ratio_ok and j < HORIZON-1:
                rearm = True
                resumes += 1
                if any_breach:
                    rearmed_after_breach = True
                active = True
        hours.append({'hour_utc_open':timestamp.isoformat(),
                      'boundary_breach':int(breach),
                      'safe_full_hour':safe,
                      'paused_or_ambiguous':not safe,
                      'close_outside':int(close > upper or close < lower),
                      'ratio_at_hour_close':None if ratio is None else round(ratio,6),
                      'ratio_passes_at_close':ratio_ok,
                      'rearm_next_hour':rearm,
                      'ratio_pause_from_next_hour':paused_by_ratio,
                      'max_wick_overshoot_pips':round(max(high-upper,lower-low,0)*10000,2)})
        prior_breach = breach
    return ({'fully_inside_active_hours':fully_active,
             'paused_or_ambiguous_hours':HORIZON-fully_active,
             'boundary_breach_hours':breach_hours,
             'fully_inside_but_paused_hours':inside_paused,
             'any_boundary_breach':any_breach,
             'first_breach_hour':first_breach_hour,
             'boundary_excursions':excursions,
             'boundary_triggered_pauses':boundary_pauses,
             'ratio_triggered_pauses':ratio_pauses,
             'resumptions':resumes,
             'reactivated_after_boundary_breach':rearmed_after_breach,
             'paused_through_last_hour':not hours[-1]['safe_full_hour'],
             'max_wick_overshoot_pips':round(max_wick_overshoot*10000,2)}, hours)


def summarize_cycles(cycles):
    n=len(cycles)
    if not n:
        return {'cycles':0}
    states=[x['state'] for x in cycles]
    safe=sum(x['fully_inside_active_hours'] for x in states)
    breached=[x for x in cycles if x['state']['any_boundary_breach']]
    stable=sum(not x['sustained_breakout_proxy'] for x in cycles)
    def count(predicate):
        return sum(bool(predicate(x)) for x in cycles)
    return {'cycles':n,
            'possible_hours':12*n,
            'fully_inside_active_hours':safe,
            'conservative_eligible_time_pct':pct(safe,12*n),
            'mean_eligible_hours_per_12h':round(safe/n,2),
            'median_eligible_hours_per_12h':statistics.median(x['fully_inside_active_hours'] for x in states),
            'paused_or_ambiguous_hours':sum(x['paused_or_ambiguous_hours'] for x in states),
            'boundary_breach_hours':sum(x['boundary_breach_hours'] for x in states),
            'fully_inside_but_paused_hours':sum(x['fully_inside_but_paused_hours'] for x in states),
            'sessions_without_any_wick_breach':n-len(breached),
            'sessions_without_any_wick_breach_pct':pct(n-len(breached),n),
            'sessions_with_any_wick_breach':len(breached),
            'sessions_with_breach_but_no_sustained_exit':count(lambda x:x['state']['any_boundary_breach'] and not x['sustained_breakout_proxy']),
            'sessions_with_sustained_exit':n-stable,
            'sessions_without_sustained_exit_pct':pct(stable,n),
            'sessions_with_any_pause':count(lambda x:x['state']['paused_or_ambiguous_hours']>0),
            'sessions_with_reactivation_after_breach':count(lambda x:x['state']['reactivated_after_boundary_breach']),
            'reactivation_among_breached_sessions_pct':pct(count(lambda x:x['state']['reactivated_after_boundary_breach']),len(breached)),
            'sessions_with_breach_no_reactivation_before_expiry':sum(not x['state']['reactivated_after_boundary_breach'] for x in breached),
            'sessions_paused_or_ambiguous_in_last_hour':count(lambda x:x['state']['paused_through_last_hour']),
            'sessions_with_ratio_invalidation':count(lambda x:x['state']['ratio_triggered_pauses']>0),
            'boundary_excursions_total':sum(x['boundary_excursions'] for x in states),
            'boundary_triggered_pause_transitions':sum(x['boundary_triggered_pauses'] for x in states),
            'ratio_triggered_pause_transitions':sum(x['ratio_triggered_pauses'] for x in states),
            'resumptions_total':sum(x['resumptions'] for x in states),
            'max_wick_overshoot_pips_across_sessions':max(x['max_wick_overshoot_pips'] for x in states),
            'median_max_wick_overshoot_pips_breached_sessions':statistics.median(x['state']['max_wick_overshoot_pips'] for x in breached) if breached else None,
           }


def main():
    if len(sys.argv)!=2:
        raise SystemExit('Usage: python src/test_pause_cycle_12h.py data/eurusd_h1.csv')
    path=Path(sys.argv[1]); h1=load_h1(path); h4=load_h4(path)
    _,_,atr,_=indicators(h4)
    split=int(len(h4)*.7)
    split_time=h4[split][0]
    end=h4[-1][0]+dt.timedelta(hours=4)
    index_by_time={row[0]:i for i,row in enumerate(h1)}
    report={'data_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'model':'Range/ATR(20 H1 range / SMA ATR14 H1) <5 at 00 UTC. Freeze preceding 80h OHLC extremes. 12h expiry, pause whenever H1 high/low breaches; all breaching hours conservatively NOT counted active. To rearm, wait for one FULLY contained completed H1 and ratio<5 at close, then resume NEXT hour. If active and ratio fails at close, pause NEXT hour. No new cycle within that 12h; next opportunity 00 UTC next day.',
            'limits':'H1 high-low identifies WHETHER a boundary was crossed but NOT its intrahour timestamp. The full crossing hour is excluded; eligible hours are fully within bounds and active at open, NOT true minutes tradable. Existing positions, orders, executions, slippage, spread, latency, PnL and liquidation NOT modeled; cannot claim risk eliminated. Historical holdout already inspected, not fresh OOS; once-daily sample misses other entry times. Sustained breakout proxy is 3 consecutive same-side closes plus 5% channel extension within 12h, not human-labelled regime truth.',
            'segments':{}}
    Path('reports').mkdir(exist_ok=True)
    for name,begin,finish in (('development',h4[200][0],split_time),('previously_inspected_2023_2026',split_time,end)):
        candidates,audit=collect(h1,h4,atr,begin,finish)
        selected=[]
        events=[]
        for episode in candidates:
            if not episode['range_atr']:
                continue
            i=index_by_time[episode['time']]
            window=h1[i:i+HORIZON]
            ratios=[range_atr_signal(h1[i-79:i+j+1]) for j in range(HORIZON)]
            state,hours=simulate_cycle(window,episode['lower'],episode['upper'],ratios)
            future=future_oracle(window,episode['lower'],episode['upper'])
            selected.append({'signal_utc':episode['time'].isoformat(),'lower':episode['lower'],
                             'upper':episode['upper'],'ratio_at_signal':episode['ratio'],
                             'sustained_breakout_proxy':future['breakout'],'state':state})
            for j,item in enumerate(hours):
                events.append({'signal_utc':episode['time'].isoformat(),'hour_number':j+1,
                               'lower':episode['lower'],'upper':episode['upper'],**item})
        metrics=summarize_cycles(selected)
        report['segments'][name]={'audit':audit,'flat_signals':len(selected),
                                 'metrics':metrics}
        print(name,json.dumps(report['segments'][name],sort_keys=True))
        with (Path('reports')/f'pause_cycle_12h_{name}.csv').open('w',newline='') as handle:
            fields=['signal_utc','lower','upper','ratio_at_signal','sustained_breakout_proxy']+list(selected[0]['state'] if selected else {})
            writer=csv.DictWriter(handle,fieldnames=fields)
            writer.writeheader()
            for row in selected:
                writer.writerow({k:v for k,v in row.items() if k!='state'}|row['state'])
        with (Path('reports')/f'pause_hours_12h_{name}.csv').open('w',newline='') as handle:
            fields=list(events[0]) if events else ['signal_utc']
            writer=csv.DictWriter(handle,fieldnames=fields)
            writer.writeheader(); writer.writerows(events)
    (Path('reports')/'pause_cycle_12h.json').write_text(json.dumps(report,indent=2)+'\n')
    print('RESEARCH ONLY: Hourly conservative availability, NOT live boundary latency, PnL, or a tradable backtest.')


if __name__=='__main__':
    main()
