"""Audit real historical L2+trades access. Download at most one hour, never use synthetic candles."""
import datetime as dt
import hashlib
import json
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = 'https://api.cryptohftdata.com/v1'
SAMPLE = '2026-09-02/12'
LIMIT = 350 * 1024 * 1024


def get(url, limit=LIMIT):
    req = urllib.request.Request(url, headers={'User-Agent':'dom-scalper-research/0.1', 'Accept':'*/*'})
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            path=Path(tempfile.gettempdir())/'dom_provider_probe.bin'
            sha=hashlib.sha256()
            size=0
            with path.open('wb') as stream:
                while True:
                    chunk=response.read(1024*1024)
                    if not chunk:break
                    size+=len(chunk)
                    if size>limit:raise ValueError('Research download safety limit exceeded')
                    stream.write(chunk);sha.update(chunk)
            return {'status':response.status,'type':response.headers.get('Content-Type',''),
                    'content_length':response.headers.get('Content-Length'), 'bytes':size,
                    'sha256':sha.hexdigest(),'path':str(path)}
    except urllib.error.HTTPError as exc:
        print('HTTP_ERROR',exc.code, url,exc.read(256).decode('utf-8','replace'),flush=True)
        return None


def main():
    print('PROBE_STARTED',dt.datetime.now(dt.timezone.utc).isoformat(),flush=True)
    for kind in ('orderbook','trades'):
        url=BASE+'/symbols?'+urllib.parse.urlencode({'exchange':'binance_futures','data_type':kind})
        result=get(url,limit=2*1024*1024)
        if result is None:
            print('SYMBOLS_UNAVAILABLE',kind,flush=True)
            continue
        raw=Path(result['path']).read_bytes()
        try:
            symbols=json.loads(raw)['symbols']
            print('SYMBOLS',kind,'count',len(symbols),'BTCUSDT',('BTCUSDT' in symbols),'ETHUSDT',('ETHUSDT' in symbols),flush=True)
        except Exception as exc:print('SYMBOLS_PARSE_ERROR',str(exc)[:150],raw[:150],flush=True)
    import pyarrow.parquet as pq
    for kind in ('orderbook','trades'):
        file=f'binance_futures/{SAMPLE}/BTCUSDT_{kind}.parquet'
        url=BASE+'/download?'+urllib.parse.urlencode({'file':file})
        result=get(url)
        if result is None:
            print('DOWNLOAD_UNAVAILABLE',file,flush=True)
            continue
        path=Path(result['path'])
        print('DOWNLOAD',file,{k:v for k,v in result.items() if k!='path'},flush=True)
        try:
            parquet=pq.ParquetFile(path)
            print('PARQUET',kind,'num_rows',parquet.metadata.num_rows,'row_groups',parquet.metadata.num_row_groups,'schema',str(parquet.schema_arrow),flush=True)
            rows=next(parquet.iter_batches(batch_size=3)).to_pylist()
            print('ROWS',kind,json.dumps(rows,default=str)[:2800],flush=True)
        except Exception as exc:
            print('PARQUET_ERROR',kind,type(exc).__name__,str(exc)[:400],flush=True)
        path.unlink(missing_ok=True)
    print('PROBE_COMPLETED',flush=True)

if __name__=='__main__':main()
