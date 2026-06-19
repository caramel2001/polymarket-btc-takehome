"""Compare all three strategies on binance-only fixtures."""
import subprocess
import json
from pathlib import Path

strategies = {
    'momentum_one_entry_hold.py': 'Momentum (one-entry hold)',
    'research/strategies/v10_btc_fairvalue_kelly.py': 'v10 BTC Fair-Value',
}

results = {}

for strategy_file, name in strategies.items():
    # Copy to model_submission
    import shutil
    shutil.copy(strategy_file, 'model_submission.py')
    
    # Run backtest
    label = name.lower().replace(' ', '_')
    result = subprocess.run(
        ['.venv/bin/python3', 'research/eval_battery.py', 
         '--model-file', 'model_submission.py', '--label', label, '--binance-only'],
        capture_output=True, text=True, timeout=300
    )
    
    # Extract key metrics
    lines = result.stdout.split('\n')
    pnl = win_rate = trades = 0
    for line in lines:
        if 'total PnL' in line:
            pnl = float(line.split('$')[1].split()[0].replace(',', ''))
        elif 'win rate' in line:
            win_rate = float(line.split(':')[1].split('%')[0])
        elif 'mean trades per' in line:
            trades = float(line.split(':')[1].split()[0])
    
    results[name] = {'pnl': pnl, 'win_rate': win_rate, 'trades': trades}
    print(f"{name:30s} | PnL: ${pnl:10,.0f} | Win: {win_rate:5.1f}% | Trades: {trades:4.1f}")

print("\n" + "="*80)
best = max(results.items(), key=lambda x: x[1]['pnl'])
print(f"✅ Best: {best[0]} with PnL=${best[1]['pnl']:,.0f}")
