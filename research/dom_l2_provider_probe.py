"""Read-only CryptoHFTData hourly BTCUSDT L2/trade feasibility probe. No orders."""
import hashlib
import json
import urllib.parse
import urllib.request
from pathlib import Path

import pyarrow.parquet as pq

BASE = 'https://api.cryptohftdata.com/v1/download'
PREFIX = 'binance_futures/2026-09-02/12/BTCUSDT_'
MAX_BYTES = 350 * 1024 * 1024


def fetch(kind):
    key = PREFIX + kind + '.parquet'
    url = BASE + '?' + urllib.parse.urlencode({'file': key})
    request = urllib.request.Request(url, headers={'User-Agent': 'dom-imbalance-research/0.2'})
    filename = Path('dom_probe_' + kind + '.parquet')
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request, timeout=90) as response, filename.open('wb') as out:
        print('HTTP', kind, response.status, response.headers.get('Content-Type'), response.headers.get('Content-Length'), flush=True)
        if response.status != 200:
            raise RuntimeError('Non-success response')
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_BYTES:
                raise RuntimeError('Download exceeds 350 MiB probe cap')
            digest.update(chunk)
            out.write(chunk)
    if size < 1024 or filename.read_bytes()[:4] != b'PAR1':
        raise RuntimeError('Not a nonempty Parquet file')
    parquet = pq.ParquetFile(filename)
    sample = next(parquet.iter_batches(batch_size=3)).to_pylist()
    result = {'source_path': key, 'size_bytes': size, 'sha256': digest.hexdigest(),
              'rows': parquet.metadata.num_rows, 'row_groups': parquet.metadata.num_row_groups,
              'schema': str(parquet.schema_arrow), 'first_rows': sample}
    Path('dom_probe_' + kind + '.json').write_text(json.dumps(result, indent=2, default=str))
    print('RESULT', kind, json.dumps(result, default=str)[:10000], flush=True)
    filename.unlink()
    return result


if __name__ == '__main__':
    outputs = {}
    for category in ('orderbook', 'trades'):
        try:
            outputs[category] = fetch(category)
        except Exception as exc:
            outputs[category] = {'error': type(exc).__name__, 'message': str(exc)}
            print('ERROR', category, type(exc).__name__, str(exc), flush=True)
    Path('dom_probe_status.json').write_text(json.dumps(outputs, indent=2, default=str))
    if not all('rows' in result and result['rows'] > 0 for result in outputs.values()):
        raise SystemExit('Real L2 and trade sample NOT accessible; no backtest performed')
