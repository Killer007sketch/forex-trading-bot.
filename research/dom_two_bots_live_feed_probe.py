"""Read-only connection audit for the two PAPER bots. Never submits orders."""
import asyncio
import json
import time
from collections import Counter
from pathlib import Path

import aiohttp
import websockets

SYMBOL = 'BTCUSDT'
SECONDS = 45
URL_PUBLIC = 'wss://fstream.binance.com/public/stream?streams=btcusdt@depth@100ms/btcusdt@bookTicker'
URL_MARKET = 'wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade'


async def run():
    status = {'symbol': SYMBOL, 'duration_requested_seconds': SECONDS, 'paper_only': True,
              'orders_sent': 0, 'start_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'counts': {}, 'errors': [], 'ready_for_bots': False}
    counts = Counter()
    last_depth_u = None
    last_trade_id = None
    first_depth = None
    first_trade = None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as client:
        for path, name in [('/fapi/v1/time', 'server_time'), ('/fapi/v1/exchangeInfo?symbol=BTCUSDT', 'exchange_info'),
                           ('/fapi/v1/depth?symbol=BTCUSDT&limit=1000', 'depth_snapshot')]:
            try:
                async with client.get('https://fapi.binance.com'+path) as resp:
                    if resp.status != 200:
                        status['errors'].append(name+':HTTP_'+str(resp.status))
                        continue
                    body = await resp.json()
                    status[name] = {'ok': True}
                    if name == 'depth_snapshot':
                        status[name]['lastUpdateId'] = body.get('lastUpdateId')
                        status[name]['bids'] = len(body.get('bids', []))
                        status[name]['asks'] = len(body.get('asks', []))
                    if name == 'exchange_info':
                        status[name]['active_contract'] = any(s.get('symbol')==SYMBOL and s.get('status')=='TRADING' and s.get('contractType')=='PERPETUAL' for s in body.get('symbols', []))
            except Exception as exc:
                status['errors'].append(name+':'+type(exc).__name__+':'+str(exc)[:180])

        async def reader(name, url):
            nonlocal first_depth, first_trade, last_depth_u, last_trade_id
            try:
                async with websockets.connect(url, open_timeout=12, ping_interval=15, ping_timeout=5, max_size=4_000_000) as ws:
                    status[name+'_connected'] = True
                    until = time.monotonic() + SECONDS
                    while time.monotonic() < until:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2)
                        except asyncio.TimeoutError:
                            status['errors'].append(name+':no_frames_2s')
                            break
                        data = json.loads(raw).get('data', {})
                        typ = data.get('e')
                        if typ == 'depthUpdate':
                            counts['depth'] += 1
                            if first_depth is None:
                                first_depth = {'U': data.get('U'), 'u':data.get('u'), 'pu':data.get('pu')}
                            elif last_depth_u is not None and data.get('pu') != last_depth_u:
                                counts['depth_sequence_mismatch'] += 1
                            last_depth_u = data.get('u')
                        elif typ == 'bookTicker':
                            counts['book_ticker'] += 1
                            try:
                                if float(data['a'])<=float(data['b']):counts['crossed_quote']+=1
                            except (ValueError, KeyError):counts['bad_quote']+=1
                        elif typ == 'aggTrade':
                            counts['agg_trade'] += 1
                            if first_trade is None:first_trade = {'a': data.get('a'), 'm': data.get('m')}
                            if last_trade_id is not None and data.get('a',0) <= last_trade_id:counts['trade_nonincreasing_id']+=1
                            last_trade_id = data.get('a')
                        elif typ is not None:
                            counts['unknown_'+str(typ)] += 1
                        else:
                            counts['non_market_frame'] += 1
            except Exception as exc:
                status['errors'].append(name+':'+type(exc).__name__+':'+str(exc)[:180])

        await asyncio.gather(reader('public', URL_PUBLIC),reader('market', URL_MARKET))
    status['counts'] = dict(counts)
    status['first_depth'] = first_depth
    status['first_trade'] = first_trade
    status['ready_for_bots'] = all(status.get(x+'_connected') for x in ('public','market')) and all(status.get(x,{}).get('ok') for x in ('server_time','exchange_info','depth_snapshot')) and all(counts[k]>0 for k in ('depth','book_ticker','agg_trade')) and counts['depth_sequence_mismatch']==0 and counts['crossed_quote']==0 and not status['errors']
    status['end_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
    Path('dom_live_feed_probe.json').write_text(json.dumps(status,indent=2,ensure_ascii=False)+'\n')
    print('LIVE_CONNECTIVITY_RESULT',json.dumps(status,ensure_ascii=False),flush=True)
    if not status['ready_for_bots']:
        raise SystemExit('NOT_READY: at least one required live feed or REST endpoint failed')

if __name__=='__main__':
    asyncio.run(run())
