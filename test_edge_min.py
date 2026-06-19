"""Test multiple EDGE_MIN values."""
import subprocess
import json
from pathlib import Path

edge_mins = [0.12, 0.15, 0.18, 0.20, 0.25]
results = {}

for edge_min in edge_mins:
    # Update model_submission.py with new EDGE_MIN
    code = open('research/strategies/v10_btc_fairvalue_kelly.py').read()
    code = code.replace('EDGE_MIN    = 0.25', f'EDGE_MIN    = {edge_min}')
    open('model_submission.py', 'w').write(code)
    
    # Run backtest
    label = f'v10_edge_{edge_min:.2f}'.replace('.', '_')
    result = subprocess.run(
        ['/home/pratham/.openclaw/workspace/polymarket-btc-takehome/.venv/bin/python3',
         'research/eval_battery.py', '--model-file', 'model_submission.py',
         '--label', label, '--binance-only'],
        capture_output=True, text=True, timeout=300
    )
    
    # Extract metrics from output
    lines = result.stdout.split('\n')
    for line in lines:
        if 'total PnL' in line:
            pnl = float(line.split('$')[1].split()[0].replace(',', ''))
            results[edge_min] = pnl
            print(f"EDGE_MIN={edge_min:.2f}: PnL=${pnl:,.2f}")
            break

# Show best
best_edge = max(results, key=lambda x: results[x])
print(f"\n🏆 Best: EDGE_MIN={best_edge:.2f} with PnL=${results[best_edge]:,.2f}")
