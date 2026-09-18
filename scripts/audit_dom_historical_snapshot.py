"""Fail-closed historical orderbook snapshot and event sequence diagnostic."""
import collections
import json
import urllib.parse
import urllib.request
from pathlib import Path
import pyarrow.parquet as pq

ROOT='https://api.cryptohftdata.com/v1/download?'
FILE='binance_futures/2026-09-02/12/BTCUSDT_orderbook.parquet'


def main():
    dest=Path('/tmp/dom_snapshot_audit.parquet')
    url=ROOT+urllib.parse.urlencode({'file':FILE})
    with urllib.request.urlopen(url,timeout=90) as response, dest.open('wb') as out:
        while True:
            part=response.read(1024*1024)
            if not part:break
            out.write(part)
    reader=pq.ParquetFile(dest)
    counts=collections.Counter()
    first={}
    seen_updates=[]
    prev_group=None
    groups=0
    missing_previous=0
    consecutive=0
    time_min=10**30
    time_max=0
    for batch in reader.iter_batches(batch_size=65536, columns=['received_time','event_type','first_update_id','final_update_id','prev_final_update_id','last_update_id','side','price','quantity']):
        c=batch.to_pydict();n=len(c['event_type'])
        for i in range(n):
            typ=c['event_type'][i];counts[typ]+=1
            ts=c['received_time'][i];time_min=min(ts,time_min);time_max=max(ts,time_max)
            if typ not in first:first[typ]={key:c[key][i] for key in c}
            if typ=='snapshot' and counts['snapshot']<=3:
                print('SNAPSHOT_SAMPLE',json.dumps({key:c[key][i] for key in c},default=str),flush=True)
            if typ=='update':
                ids=(c['first_update_id'][i],c['final_update_id'][i],c['prev_final_update_id'][i],ts)
                if ids!=prev_group:
                    groups+=1
                    if prev_group is not None:
                        if ids[2] is None:missing_previous+=1
                        elif ids[2]==prev_group[1]:consecutive+=1
                    if len(seen_updates)<5:seen_updates.append(ids)
                    prev_group=ids
    print('SUMMARY',json.dumps({'file':FILE,'row_count':reader.metadata.num_rows,'event_types':counts,'first_by_type':first,'update_groups':groups,'continuous_prev_update_links':consecutive,'missing_prev_update_links':missing_previous,'first_five_update_groups':seen_updates,'first_received_ns':time_min,'last_received_ns':time_max},default=str),flush=True)
    if not counts['snapshot']:
        print('DATA_UNUSABLE_FOR_STANDALONE_REPLAY: NO_SNAPSHOT_IN_THIS_HOUR',flush=True)
    dest.unlink(missing_ok=True)

if __name__=='__main__':main()
