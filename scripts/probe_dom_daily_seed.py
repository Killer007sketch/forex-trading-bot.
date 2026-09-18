"""Fast vectorized inspect: hourly snapshots at day boundary, never reconstruct without a true seed."""
import collections
import datetime
import json
import urllib.parse
import urllib.request
from pathlib import Path
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

HOST='https://api.cryptohftdata.com/v1/download?'
HOURS=['2026-09-02/00','2026-09-02/01','2026-09-01/23']

def main():
    for at in HOURS:
        file=f'binance_futures/{at}/BTCUSDT_orderbook.parquet'
        url=HOST+urllib.parse.urlencode({'file':file})
        path=Path('/tmp/seed_'+at.replace('/','_')+'.parquet')
        try:
            with urllib.request.urlopen(url,timeout=90) as response,path.open('wb') as dst:
                while True:
                    b=response.read(1024*1024)
                    if not b:break
                    dst.write(b)
            reader=pq.ParquetFile(path)
            types=reader.read(columns=['event_type'])['event_type']
            counts={str(d['values'].as_py()):int(d['counts'].as_py()) for d in pc.value_counts(types)}
            print('SEED_HOUR',at,'compressed_bytes',path.stat().st_size,'rows',reader.metadata.num_rows,'event_types',counts,flush=True)
            if counts.get('snapshot',0):
                # Extract snapshot time and last_update_id for bridging.
                first=None
                for batch in reader.iter_batches(batch_size=32768,columns=['received_time','event_type','last_update_id','side','price','quantity']):
                    cols=batch.to_pydict()
                    for i,kind in enumerate(cols['event_type']):
                        if kind=='snapshot':
                            first={key:cols[key][i] for key in cols}
                            break
                    if first:break
                print('FIRST_SNAPSHOT',at,json.dumps(first),flush=True)
            path.unlink(missing_ok=True)
        except Exception as exc:
            print('SEED_HOUR_ERROR',at,type(exc).__name__,str(exc)[:300],flush=True)
            path.unlink(missing_ok=True)
    print('SEED_PROBE_DONE',datetime.datetime.now(datetime.timezone.utc).isoformat(),flush=True)

if __name__=='__main__':main()
