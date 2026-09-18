"""Independent sample scan: do documented historical L2 files contain reconstructable snapshots?"""
import json
import urllib.parse
import urllib.request
from pathlib import Path
import pyarrow.parquet as pq
import pyarrow.compute as pc

BASE='https://api.cryptohftdata.com/v1/download'
SAMPLES=[('2026-09-01',0,'BTCUSDT'),('2026-09-02',0,'BTCUSDT'),
         ('2026-09-02',1,'BTCUSDT'),('2026-09-02',6,'BTCUSDT'),
         ('2026-09-02',18,'BTCUSDT'),('2026-09-02',23,'BTCUSDT'),
         ('2026-09-02',0,'ETHUSDT'),('2026-09-02',12,'ETHUSDT')]
results=[]
for date,hour,symbol in SAMPLES:
    key=f'binance_futures/{date}/{hour:02d}/{symbol}_orderbook.parquet'
    url=BASE+'?'+urllib.parse.urlencode({'file':key})
    file=Path('snapshot_scan_download.parquet')
    result={'source':key}
    try:
        with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'dom-audit/0.4'}),timeout=90) as r,file.open('wb') as out:
            if r.status!=200:raise RuntimeError(f'HTTP {r.status}')
            while True:
                chunk=r.read(1024*1024)
                if not chunk:break
                out.write(chunk)
                if out.tell()>120*1024*1024:raise RuntimeError('Above 120 MiB cap')
        parquet=pq.ParquetFile(file)
        counts={}
        snapshots=0; first_type=None
        for batch in parquet.iter_batches(batch_size=500000,columns=['event_type','last_update_id']):
            et=batch.column(0)
            for element in pc.value_counts(et).to_pylist():
                counts[element['values']]=counts.get(element['values'],0)+element['counts']
            snapshots+=pc.count(pc.filter(batch.column(1),pc.equal(et,'snapshot'))).as_py()
            if first_type is None:first_type=et[0].as_py()
        result.update({'bytes':file.stat().st_size,'rows':parquet.metadata.num_rows,
                       'event_type_counts':counts,'snapshot_rows_with_id':snapshots,'first_type':first_type})
    except Exception as exc:
        result['error']=type(exc).__name__+': '+str(exc)
    finally:
        file.unlink(missing_ok=True)
    results.append(result)
    print('SCAN',json.dumps(result),flush=True)
Path('dom_snapshot_scan.json').write_text(json.dumps(results,indent=2)+'\n')
if not any(r.get('event_type_counts',{}).get('snapshot',0)>0 for r in results):
    raise SystemExit('No snapshot in eight sampled files: cannot claim executable full-depth L2 backtest')
