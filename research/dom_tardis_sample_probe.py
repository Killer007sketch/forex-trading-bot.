"""Probe legitimate first-of-month public Binance Futures L2 snapshots/trades.
Read only capped response prefix, no exchange credentials, no trading.
"""
import json
import urllib.error
import urllib.request
import zlib
from pathlib import Path

BASE='https://datasets.tardis.dev/v1/binance-futures/'
RESULTS=[]
for symbol in ('BTCUSDT','ETHUSDT'):
    for kind in ('book_snapshot_25','incremental_book_L2','trades'):
        url=f'{BASE}{kind}/2026/09/01/{symbol}.csv.gz'
        item={'symbol':symbol,'kind':kind,'url':url}
        try:
            request=urllib.request.Request(url,headers={'User-Agent':'DOM-research/0.5','Range':'bytes=0-65535'})
            with urllib.request.urlopen(request,timeout=45) as res:
                chunk=res.read(65536)
                item.update({'http_status':res.status,'content_length_header':res.headers.get('Content-Length'),
                    'content_range_header':res.headers.get('Content-Range'),'content_type':res.headers.get('Content-Type'),
                    'content_encoding':res.headers.get('Content-Encoding'),'received_bytes':len(chunk),
                    'gzip_magic':chunk[:2].hex()})
            if chunk[:2]==b'\x1f\x8b':
                out=zlib.decompressobj(31).decompress(chunk,100000)
                item['csv_prefix']=out.decode('utf-8','replace').splitlines()[:3]
            else:item['response_prefix']=chunk[:200].decode('utf-8','replace')
        except Exception as exc:
            item['error']=type(exc).__name__+': '+str(exc)
        RESULTS.append(item)
        print('TARDIS',json.dumps(item),flush=True)
Path('dom_tardis_probe.json').write_text(json.dumps(RESULTS,indent=2)+'\n')
if not all(item.get('gzip_magic')=='1f8b' for item in RESULTS):
    raise SystemExit('Cannot access all first-of-month snapshot+trades files without credentials')
