"""Audit real provider parquet order book and trade continuity, without trading."""
import collections
import hashlib
import json
import urllib.parse
import urllib.request
from pathlib import Path

import pyarrow.parquet as pq

BASE='https://api.cryptohftdata.com/v1/download'
PREFIX='binance_futures/2026-09-02/12/BTCUSDT_'


def download(kind):
    key=PREFIX+kind+'.parquet'
    url=BASE+'?'+urllib.parse.urlencode({'file':key})
    dest=Path('dom_quality_'+kind+'.parquet')
    with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'dom-audit/0.3'}),timeout=90) as r, dest.open('wb') as f:
        if r.status!=200:raise RuntimeError('Download rejected')
        while True:
            chunk=r.read(1024*1024)
            if not chunk:break
            f.write(chunk)
    if dest.stat().st_size<1024 or dest.open('rb').read(4)!=b'PAR1':raise ValueError('Missing genuine parquet')
    return dest


def inspect_book(file):
    pf=pq.ParquetFile(file)
    cols=['received_time','event_type','first_update_id','final_update_id','prev_final_update_id','last_update_id','side','price','quantity']
    count=collections.Counter()
    first=None;last_ts=None;prev_key=None;last_u=None;last_snapshot=None
    group_rows=0;events=0;max_gap=0;first_snap_event=None;sequence_errors=[];examples=[]
    def commit(key,n):
        nonlocal events,last_u,first_snap_event,last_snapshot
        if key is None:return
        ts,etype,U,u,pu,snapshot_id=key
        events+=1;count['groups_'+str(etype)]+=1
        if etype=='snapshot':
            if first_snap_event is None:first_snap_event=events
            last_snapshot=ts
            last_u=snapshot_id
        elif etype=='update':
            if last_snapshot is not None:
                if pu is not None and last_u is not None and pu!=last_u and len(sequence_errors)<12:
                    sequence_errors.append({'event':events,'timestamp_ns':ts,'previous_u':last_u,'pu':pu,'U':U,'u':u})
                if pu is None and U is not None and last_u is not None and U>last_u+1 and len(sequence_errors)<12:
                    sequence_errors.append({'event':events,'timestamp_ns':ts,'previous_u':last_u,'U':U,'u':u})
            if u is not None:last_u=u
        if len(examples)<6:examples.append({'event':events,'timestamp_ns':ts,'type':etype,'U':U,'u':u,'pu':pu,'snapshot_id':snapshot_id,'levels':n})
    for batch in pf.iter_batches(batch_size=30000,columns=cols):
        vals=batch.to_pydict()
        for i,ts in enumerate(vals['received_time']):
            key=(ts,vals['event_type'][i],vals['first_update_id'][i],vals['final_update_id'][i],vals['prev_final_update_id'][i],vals['last_update_id'][i])
            if first is None:first=ts
            if last_ts is not None and ts<last_ts:count['timestamp_reversals']+=1
            if prev_key is not None and key!=prev_key:
                commit(prev_key,group_rows)
                group_rows=0
            if last_ts is not None and ts>last_ts:max_gap=max(max_gap,(ts-last_ts)//1000000)
            prev_key=key;group_rows+=1;last_ts=ts
            count['level_'+str(vals['side'][i])]+=1
    commit(prev_key,group_rows)
    return {'rows':pf.metadata.num_rows,'events':events,'counts':dict(count),'first_ts_ns':first,'last_ts_ns':last_ts,
            'max_level_time_gap_ms':max_gap,'first_snapshot_group':first_snap_event,'last_snapshot_ns':last_snapshot,
            'continuity_errors_examples':sequence_errors,'first_event_examples':examples}


def inspect_trades(file):
    pf=pq.ParquetFile(file)
    first=None;last=None;max_gap=0;backward=0;positive=0
    for batch in pf.iter_batches(batch_size=100000,columns=['received_time','quantity']):
        x=batch.to_pydict()
        for ts,q in zip(x['received_time'],x['quantity']):
            if first is None:first=ts
            if last is not None:
                if ts<last:backward+=1
                else:max_gap=max(max_gap,(ts-last)//1000000)
            if float(q)>0:positive+=1
            last=ts
    return {'rows':pf.metadata.num_rows,'first_ts_ns':first,'last_ts_ns':last,'max_trade_gap_ms':max_gap,
            'timestamp_reversals':backward,'positive_quantity_rows':positive}


if __name__=='__main__':
    output={}
    try:
        for kind in ('orderbook','trades'):
            path=download(kind)
            output[kind]=inspect_book(path) if kind=='orderbook' else inspect_trades(path)
            print('QUALITY',kind,json.dumps(output[kind])[:12000],flush=True)
            path.unlink()
    finally:
        Path('dom_l2_quality.json').write_text(json.dumps(output,indent=2)+'\n')
    if output['orderbook']['first_snapshot_group'] is None or output['orderbook']['counts'].get('groups_update',0)==0:
        raise SystemExit('Cannot reconstruct: snapshot or deltas missing')
    if output['trades']['rows']==0:raise SystemExit('No trades')
