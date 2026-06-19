"""Replay v10 on all binance fixtures and extract per-trade entry details."""
import sys
sys.path.insert(0, 'src')
import math
import pandas as pd
from pathlib import Path
from polybench.cli import _load_model_from_file
from polybench.replay import ReplayConfig, replay

SIGMA_1S = 0.719e-4
LOGIT_A = 0.029
LOGIT_B = 1.474

def p_fair(btc, btc_open, ttr):
    z = math.log(btc / btc_open) / (SIGMA_1S * math.sqrt(max(ttr, 1.0)))
    return 1.0 / (1.0 + math.exp(-(LOGIT_A + LOGIT_B * z)))

fixtures = sorted(Path('data/live_recordings').glob('*.parquet'))
# binance only - check first row
binance_fixtures = []
for f in fixtures:
    df = pd.read_parquet(f, columns=['btc_last'])
    if (df['btc_last'] > 0).mean() > 0.5:
        binance_fixtures.append(f)

print(f"Binance fixtures: {len(binance_fixtures)}")

trades = []

for fpath in binance_fixtures:
    df = pd.read_parquet(fpath)
    for event_id, g in df.groupby('event_id'):
        g = g.reset_index(drop=True)
        btc_vals = g['btc_last'].values
        first_btc = None
        for i, row in g.iterrows():
            btc = row['btc_last']
            ttr = row['time_to_resolve']
            if first_btc is None and btc > 0:
                first_btc = btc
            if first_btc is None:
                continue
            if not (40 <= ttr <= 240):
                continue
            move_bps = abs(math.log(btc / first_btc)) * 1e4
            if move_bps < 6.0:
                continue
            p = p_fair(btc, first_btc, ttr)
            up_ask = row['up_ask']
            down_ask = row['down_ask']
            side = None
            if p - up_ask > 0.25 and 0.05 < up_ask < 0.97:
                side, p_win, ask = 'UP', p, up_ask
            elif (1-p) - down_ask > 0.25 and 0.05 < down_ask < 0.97:
                side, p_win, ask = 'DOWN', 1-p, down_ask
            if side is None:
                continue
            # found entry; record resolution
            resolved = row['resolved_outcome'] if 'resolved_outcome' in row else g.iloc[-1].get('resolved_outcome', '')
            res_outcome = g.iloc[-1]['resolved_outcome'] if 'resolved_outcome' in g.columns else None
            won = (side == 'UP' and res_outcome == 'UP') or (side == 'DOWN' and res_outcome == 'DOWN')
            trades.append({
                'fixture': fpath.name,
                'event_id': event_id,
                'ttr': ttr,
                'move_bps': move_bps,
                'p_fair': p,
                'side': side,
                'p_win': p_win,
                'ask': ask,
                'edge': p_win - ask,
                'won': won,
                'res_outcome': res_outcome,
                'btc': btc,
                'first_btc': first_btc,
            })
            break  # only first trigger per event

import pandas as pd
tdf = pd.DataFrame(trades)
print(f"\nTotal trade triggers: {len(tdf)}")
print(f"Win rate: {tdf['won'].mean()*100:.1f}%")
print(f"\nBy TTR bucket:")
tdf['ttr_bucket'] = pd.cut(tdf['ttr'], bins=[40,80,120,160,200,240])
print(tdf.groupby('ttr_bucket')['won'].agg(['mean','count']))

print(f"\nBy move_bps bucket:")
tdf['move_bucket'] = pd.cut(tdf['move_bps'], bins=[6,10,15,20,30,50,200])
print(tdf.groupby('move_bucket')['won'].agg(['mean','count']))

print(f"\nBy edge bucket:")
tdf['edge_bucket'] = pd.cut(tdf['edge'], bins=[0,0.25,0.30,0.35,0.40,0.50,1.0])
print(tdf.groupby('edge_bucket')['won'].agg(['mean','count']))

print(f"\nBy ask bucket:")
tdf['ask_bucket'] = pd.cut(tdf['ask'], bins=[0,0.1,0.2,0.3,0.5,0.7,1.0])
print(tdf.groupby('ask_bucket')['won'].agg(['mean','count']))

tdf.to_csv('research/results/v10_trade_analysis.csv', index=False)
print("\nSaved to research/results/v10_trade_analysis.csv")
