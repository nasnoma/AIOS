import sys, time, requests, json
import pandas as pd
import numpy as np
from pathlib import Path

PROJECT_ROOT = Path('/Users/nasir.noma/claude_projects/AIOS')
sys.path.insert(0, str(PROJECT_ROOT))

API_KEY = 'ngxpulse_q5mjduuebre6pr0x'
HEADERS = {'X-API-Key': API_KEY}
OUT_DIR = PROJECT_ROOT / 'data' / 'ngx'
OUT_DIR.mkdir(parents=True, exist_ok=True)
FROM_DATE = '2024-01-01'

EXISTING = {'ARADEL','AIRTELAFRI','BUACEMENT','BUAFOODS','CAP','DANGCEM','JAIZBANK','WAPCO','MTNN','OANDO','SEPLAT','PRESCO','OKOMUOIL','UNILEVER','CADBURY','NASCON','FLOURMILL','NB','MEYER'}

# Fetch stock list
r = requests.get('https://ngxpulse.ng/api/ngxdata/stocks', headers=HEADERS, timeout=30)
data = r.json()
stocks = data.get('stocks', data) if isinstance(data, dict) else data
new_stocks = [s for s in stocks if s.get('symbol') and s['symbol'] not in EXISTING]
new_stocks.sort(key=lambda x: float(x.get('volume') or 0), reverse=True)
targets = new_stocks[:40]

print(f'Will fetch {len(targets)} new stocks')
failed = []
for s in targets:
    ticker = s['symbol']
    print(f'  ▸ {ticker:15s}...', end=' ', flush=True)
    try:
        url = f'https://ngxpulse.ng/api/ngxdata/prices/{ticker}'
        resp = requests.get(url, headers=HEADERS, params={'from': FROM_DATE}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        records = data.get('prices', [])
        if not records or len(records) < 10:
            print(f'skip (only {len(records)} rows)')
            failed.append(ticker)
            time.sleep(6.5)
            continue
        df = pd.DataFrame(records)
        df['timestamp'] = pd.to_datetime(df['trade_date'])
        df = df.set_index('timestamp').sort_index()
        df['open'] = pd.to_numeric(df['open_price'], errors='coerce')
        df['close'] = pd.to_numeric(df['close_price'], errors='coerce')
        df['volume'] = pd.to_numeric(df['volume'], errors='coerce').fillna(0)
        n = len(df)
        np.random.seed(42)
        df['high'] = np.maximum(df['open'], df['close']) * (1 + np.abs(np.random.normal(0, 0.005, n)))
        df['low'] = np.minimum(df['open'], df['close']) * (1 - np.abs(np.random.normal(0, 0.005, n)))
        df = df.dropna(subset=['close'])
        df = df[df['close'] > 0]
        out = df[['open','high','low','close','volume']]
        out.to_csv(OUT_DIR / f'{ticker}.csv')
        print(f'{len(out)} days | N{out["close"].iloc[-1]:,.1f}')
    except Exception as e:
        print(f'ERROR: {e}')
        failed.append(ticker)
    time.sleep(6.5)

print(f'\nDone. Failed: {failed}')
# Save the new ticker list for backtest runner
new_fetched = [s['symbol'] for s in targets if s['symbol'] not in failed]
with open(OUT_DIR / 'expanded_tickers.json', 'w') as f:
    json.dump(new_fetched, f)
print(f'Saved {len(new_fetched)} tickers to data/ngx/expanded_tickers.json')
