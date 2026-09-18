"""Check anonymous availability off free first-of-month; no secret or account access."""
import gzip,json,urllib.parse,urllib.request,urllib.error
BASE='https://api.tardis.dev/v1/data-feeds/binance-futures'
filters=[{'channel':'depthSnapshot','symbols':['btcusdt']},{'channel':'depth','symbols':['btcusdt']},{'channel':'aggTrade','symbols':['btcusdt']}]
for date in ('2026-09-01','2026-09-02','2026-09-03'):
 url=BASE+'?'+urllib.parse.urlencode({'from':date,'offset':0,'sliceSize':1,'filters':json.dumps(filters,separators=(',',':')),'compression':'gzip'})
 req=urllib.request.Request(url,headers={'Accept-Encoding':'gzip','User-Agent':'dom-paper-audit/1.0'})
 try:
  with urllib.request.urlopen(req,timeout=40) as resp:
   blob=resp.read(2*1024*1024+1)
   print('ACCESS',date,'http',resp.status,'compressed_bytes_read',len(blob),'content_encoding',resp.headers.get('Content-Encoding'),flush=True)
   if len(blob)<2*1024*1024 and resp.headers.get('Content-Encoding')=='gzip':
    raw=gzip.decompress(blob);print('NATIVE_EVENTS',date,len(raw.splitlines()),flush=True)
 except urllib.error.HTTPError as exc:
  print('ACCESS_DENIED',date,'http',exc.code,'body',exc.read(256).decode('utf-8','replace'),flush=True)
 except Exception as exc:
  print('ACCESS_ERROR',date,type(exc).__name__,str(exc)[:150],flush=True)
