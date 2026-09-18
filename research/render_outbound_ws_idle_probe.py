"""Render Free idle experiment: outbound OKX WebSocket ONLY, no trading/API keys.

Do NOT poll /status or configure an HTTP health check during the 20-min idle test:
HTTP requests invalidate the experiment. Default Render TCP port checks are fine.
"""
import asyncio
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
import uuid

import websockets

BOOT = time.time()
BOOT_ID = uuid.uuid4().hex
LOCK = threading.Lock()
STATE = dict(boot_id=BOOT_ID, boot_utc=datetime.fromtimestamp(BOOT, timezone.utc).isoformat(),
             venue='OKX', symbol='BTC-USDT-SWAP', paper_only=True, orders_sent=0,
             connected=False, connection_count=0, trade_messages=0, trade_rows=0,
             last_trade_utc=None, last_error=None)


def emit(kind, **fields):
    print(json.dumps({'event': kind, 'ts_utc': datetime.now(timezone.utc).isoformat(),
                      'boot_id': BOOT_ID, **fields}), flush=True)


def snapshot():
    with LOCK:
        result = STATE.copy()
    result['uptime_seconds'] = round(time.time() - BOOT, 1)
    return result


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ('/', '/status'):
            self.send_error(404)
            return
        raw = json.dumps(snapshot()).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        pass  # HTTP access is not used as a keepalive.


async def consume_okx():
    url = 'wss://ws.okx.com:8443/ws/v5/public'
    delay = 1
    while True:
        try:
            async with websockets.connect(url, open_timeout=12, ping_interval=20,
                                          ping_timeout=20, max_size=2_000_000) as ws:
                await ws.send(json.dumps({'op': 'subscribe', 'args': [
                    {'channel': 'trades', 'instId': 'BTC-USDT-SWAP'}]}))
                with LOCK:
                    STATE['connected'] = True
                    STATE['connection_count'] += 1
                    STATE['last_error'] = None
                emit('connected', connection_count=snapshot()['connection_count'])
                delay = 1
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=25)
                    except asyncio.TimeoutError:
                        await ws.send('ping')  # OKX protocol keepalive, NOT HTTP to Render.
                        continue
                    if raw == 'pong':
                        continue
                    msg = json.loads(raw)
                    if msg.get('event') == 'error':
                        raise RuntimeError('OKX subscription error: ' + str(msg)[:160])
                    if msg.get('arg', {}).get('channel') != 'trades':
                        continue
                    rows = msg.get('data', [])
                    with LOCK:
                        STATE['trade_messages'] += 1
                        STATE['trade_rows'] += len(rows)
                        STATE['last_trade_utc'] = datetime.now(timezone.utc).isoformat()
        except Exception as exc:
            with LOCK:
                STATE['connected'] = False
                STATE['last_error'] = type(exc).__name__ + ': ' + str(exc)[:160]
            emit('reconnect', error=snapshot()['last_error'], backoff_seconds=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


async def log_heartbeats():
    while True:
        await asyncio.sleep(60)
        emit('minute_heartbeat', **snapshot())


def run_worker():
    asyncio.run(asyncio.gather(consume_okx(), log_heartbeats()))


if __name__ == '__main__':
    # asyncio.gather must be called within a running event loop in Python 3.11+.
    async def worker():
        await asyncio.gather(consume_okx(), log_heartbeats())
    port = int(os.getenv('PORT', '10000'))
    emit('start', port=port, experiment='outbound OKX traffic vs Render free idle')
    threading.Thread(target=lambda: asyncio.run(worker()), daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', port), Handler).serve_forever()
