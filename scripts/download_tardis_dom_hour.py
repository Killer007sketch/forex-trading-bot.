"""Save genuine free first-day historical 60-minute Binance futures raw events.

One gzip member per 10 minutes, preserving native Tardis capture ordering.
NO fake snapshots, OHLCV or synthetic fills. No API keys required.
"""
import datetime
import gzip
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT='https://api.tardis.dev/v1/data-feeds/binance-futures'
FILTERS=[{'channel':'depthSnapshot','symbols':['btcusdt']},{'channel':'depth','symbols':['btcusdt']},{'channel':'aggTrade','symbols':['btcusdt']}]
OUT=Path('data/tardis_BTCUSDT_2026-09-01_00-01_native.ndjson.gz')


def main():
    OUT.parent.mkdir(parents=True,exist_ok=True)
    checksum=hashlib.sha256()
    total=0
    slices=[]
    with OUT.open('wb') as dst:
        for offset in range(0,60,10):
            args={'from':'2026-09-01','offset':offset,'sliceSize':10,'filters':json.dumps(FILTERS,separators=(',',':')),'compression':'gzip'}
            url=ROOT+'?'+urllib.parse.urlencode(args)
            for attempt in range(4):
                try:
                    with urllib.request.urlopen(urllib.request.Request(url,headers={'Accept-Encoding':'gzip','User-Agent':'dom-scalper-research/0.2'}),timeout=120) as response:
                        if response.status!=200 or response.headers.get('Content-Encoding')!='gzip':
                            raise RuntimeError('Unexpected status or encoding')
                        data=response.read(20*1024*1024+1)
                        if len(data)>20*1024*1024:raise RuntimeError('Slice >20MiB safety cap')
                        decoded=gzip.decompress(data)
                        if not decoded.strip():raise RuntimeError('Empty archive slice')
                        if not decoded.endswith(b'\n'):raise RuntimeError('Truncated NDJSON chunk')
                        break
                except urllib.error.HTTPError as exc:
                    if exc.code!=429 or attempt==3:raise
                    time.sleep(int(exc.headers.get('Retry-After','5')))
            # Every chunk must contain >=1 valid depth or trade frame; gaps are fail closed in replay.
            if b'@depth' not in decoded or b'@aggTrade' not in decoded:
                raise RuntimeError('Missing depth/trades in raw slice')
            dst.write(data);dst.flush();checksum.update(data);total+=len(data)
            summary={'offset_minute':offset,'compressed_bytes':len(data),'native_lines':len(decoded.splitlines()),'sha256':hashlib.sha256(data).hexdigest()}
            slices.append(summary)
            print('SAVED',json.dumps(summary),flush=True)
    meta={'source':'Tardis.dev native historical Binance USDT perpetual','date':'2026-09-01','start_utc':'00:00:00','end_utc':'01:00:00',
          'instrument':'BTCUSDT','is_real_market_data':True,'includes_true_seed':True,'sample_is_not_a_month':True,
          'file':str(OUT),'compressed_bytes':total,'sha256':checksum.hexdigest(),'slices':slices}
    Path('data/tardis_hour_provenance.json').write_text(json.dumps(meta,indent=2)+'\n')
    print('ONE_HOUR_COMPLETE',json.dumps(meta),flush=True)

if __name__=='__main__':main()
