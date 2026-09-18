"""Tardis free first-of-month API: verify genuine depthSnapshot, depth + aggTrade."""
import datetime
import gzip
import json
import urllib.parse
import urllib.request
from collections import Counter

BASE='https://api.tardis.dev/v1/data-feeds/binance-futures'
FILTERS=[{'channel':'depthSnapshot','symbols':['btcusdt']},{'channel':'depth','symbols':['btcusdt']},{'channel':'aggTrade','symbols':['btcusdt']}]


def main():
    params={'from':'2026-09-01','offset':0,'sliceSize':1,'filters':json.dumps(FILTERS,separators=(',',':')),'compression':'gzip'}
    url=BASE+'?'+urllib.parse.urlencode(params)
    request=urllib.request.Request(url,headers={'User-Agent':'dom-historical-replay-probe/0.1','Accept-Encoding':'gzip'})
    print('PROBING_FIRST_DAY_2026_09',flush=True)
    with urllib.request.urlopen(request,timeout=90) as response:
        payload=response.read(80*1024*1024+1)
        if len(payload)>80*1024*1024:raise ValueError('Probe exceeds 80 MiB compressed limit')
        print('HTTP',response.status,'encoding',response.headers.get('Content-Encoding'),'compressed_bytes',len(payload),flush=True)
        if response.headers.get('Content-Encoding')=='gzip':payload=gzip.decompress(payload)
    counts=Counter();first={};snapshot_last=None;gap=0
    for line in payload.splitlines():
        if not line.strip():counts['disconnect']+=1;continue
        stamp, _, raw=line.partition(b' ')
        try:msg=json.loads(raw)
        except Exception as exc:raise ValueError('Invalid exchange-native message') from exc
        data=msg.get('data',msg)
        name=msg.get('stream',data.get('e','other'))
        if 'depthSnapshot' in name:kind='snapshot'
        elif data.get('e')=='depthUpdate':kind='depth'
        elif data.get('e')=='aggTrade':kind='trade'
        else:kind='other'
        counts[kind]+=1
        if kind not in first:first[kind]={'local_time':stamp.decode(),'message':msg}
        if kind=='snapshot':snapshot_last=data.get('lastUpdateId')
    print('REAL_MINUTE_SUMMARY',json.dumps({'counts':counts,'first':first,'snapshot_last_update_id':snapshot_last,'uncompressed_bytes':len(payload)},default=str)[:9000],flush=True)
    if counts['snapshot']==0:print('FAIL_CLOSED_NO_SNAPSHOT',flush=True)
    if counts['depth']==0 or counts['trade']==0:print('FAIL_CLOSED_MISSING_DEPTH_OR_TRADES',flush=True)

if __name__=='__main__':main()
