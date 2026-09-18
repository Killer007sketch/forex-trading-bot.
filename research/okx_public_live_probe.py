"""Read-only OKX BTC-USDT-SWAP public SWAP metadata + books/trades probe. No orders."""
import asyncio
import json
import time
from collections import Counter
from pathlib import Path
import aiohttp
import websockets

async def main():
    report={'venue':'OKX','symbol':'BTC-USDT-SWAP','paper_only':True,'orders':0,'counts':{},'errors':[]}
    count=Counter()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as client:
        try:
            async with client.get('https://www.okx.com/api/v5/public/instruments',params={'instType':'SWAP','instId':'BTC-USDT-SWAP'}) as resp:
                report['rest_http']=resp.status
                metadata=await resp.json() if resp.status==200 else {}
                instruments=[i for i in metadata.get('data',[]) if i.get('instId')=='BTC-USDT-SWAP' and i.get('state')=='live']
                if instruments:
                    ins=instruments[0]
                    report['contract']={k:ins.get(k) for k in ('ctVal','ctValCcy','ctType','tickSz','lotSz','minSz','state')}
                else:report['errors'].append('not_live_swap_or_no_metadata')
        except Exception as exc:report['errors'].append('REST:'+type(exc).__name__+':'+str(exc)[:150])
    try:
        async with websockets.connect('wss://ws.okx.com:8443/ws/v5/public',open_timeout=12,ping_interval=None,max_size=4_000_000) as ws:
            report['websocket_connected']=True
            await ws.send(json.dumps({'op':'subscribe','args':[{'channel':'books','instId':'BTC-USDT-SWAP'},{'channel':'trades','instId':'BTC-USDT-SWAP'}]}))
            end=time.monotonic()+18
            last_seq=None
            while time.monotonic()<end:
                try:raw=await asyncio.wait_for(ws.recv(),timeout=3)
                except asyncio.TimeoutError:
                    await ws.send('ping')
                    count['ping_sent']+=1
                    continue
                if raw=='pong':count['pong']+=1;continue
                msg=json.loads(raw)
                if msg.get('event'):
                    count['control_'+str(msg['event'])]+=1
                    if msg['event']=='error':report['errors'].append('subscribe_error:'+str(msg)[:200])
                    continue
                channel=msg.get('arg',{}).get('channel')
                for item in msg.get('data',[]):
                    if channel=='books':
                        count['books']+=1
                        if msg.get('action')=='snapshot':count['snapshots']+=1
                        if msg.get('action')=='update':count['updates']+=1
                        if last_seq is not None and item.get('prevSeqId') is not None and int(item['prevSeqId'])!=last_seq:count['seq_mismatches']+=1
                        if item.get('seqId') is not None:last_seq=int(item['seqId'])
                        bids=item.get('bids',[]);asks=item.get('asks',[])
                        if bids and asks:
                            count['two_sided_books']+=1
                            if float(bids[0][0])>=float(asks[0][0]):count['crossed_book']+=1
                    elif channel=='trades':
                        count['trades']+=1
                        if item.get('side') not in ('buy','sell'):count['bad_trade_side']+=1
                    else:count['unknown_channel']+=1
    except Exception as exc:report['errors'].append('WSS:'+type(exc).__name__+':'+str(exc)[:200])
    report['counts']=dict(count)
    report['ready_for_adapter']=bool(report.get('contract') and report.get('websocket_connected') and count['snapshots']>=1 and count['two_sided_books']>0 and count['trades']>0 and count['seq_mismatches']==0 and count['crossed_book']==0 and not report['errors'])
    Path('okx_live_probe.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    print('OKX_PROBE',json.dumps(report,ensure_ascii=False),flush=True)
    if not report['ready_for_adapter']:raise SystemExit('Not ready for OKX adapter')

if __name__=='__main__':asyncio.run(main())
