import subprocess
import re

base_code = open('research/strategies/v10_btc_fairvalue_kelly.py').read()
base_code = base_code.replace('ASK_LO      = 0.05', 'ASK_LO      = 0.30')

configs = [
    ('baseline_ask030', {}),
    ('ttr_40_180', {'ENTRY_TTR_HI = 240.0': 'ENTRY_TTR_HI = 180.0'}),
    ('ttr_60_240', {'ENTRY_TTR_LO = 40.0': 'ENTRY_TTR_LO = 60.0'}),
    ('move_min_8', {'BTC_MOVE_MIN_BPS = 6.0': 'BTC_MOVE_MIN_BPS = 8.0'}),
    ('move_min_10', {'BTC_MOVE_MIN_BPS = 6.0': 'BTC_MOVE_MIN_BPS = 10.0'}),
    ('kelly_full', {'KELLY_FRACTION = 0.50': 'KELLY_FRACTION = 1.00'}),
    ('kelly_quarter', {'KELLY_FRACTION = 0.50': 'KELLY_FRACTION = 0.25'}),
]

results = {}
for name, replacements in configs:
    code = base_code
    for old, new in replacements.items():
        code = code.replace(old, new)
    open('model_submission.py', 'w').write(code)

    result = subprocess.run(
        ['.venv/bin/python3', 'research/eval_battery.py',
         '--model-file', 'model_submission.py', '--label', f'sweep_{name}', '--binance-only'],
        capture_output=True, text=True, timeout=300
    )
    out = result.stdout
    pnl_m = re.search(r'total PnL across fixtures\s*:\s*\$\s*([-\d,\.]+)', out)
    win_m = re.search(r'win rate vs baseline\s*:\s*([\d.]+)%', out)
    trades_m = re.search(r'mean trades per fixture\s*:\s*([\d.]+)', out)
    pnl = float(pnl_m.group(1).replace(',','')) if pnl_m else None
    win = float(win_m.group(1)) if win_m else None
    trades = float(trades_m.group(1)) if trades_m else None
    results[name] = (pnl, win, trades)
    print(f"{name:20s} | PnL: ${pnl:10,.2f} | Win: {win:5.1f}% | Trades: {trades:4.2f}")

print("\nBest:", max(results.items(), key=lambda x: x[1][0]))
