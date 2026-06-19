import subprocess
import re

base_code = open('research/strategies/v10_btc_fairvalue_kelly.py').read()

configs = [
    ('ask030_move8', {'ASK_LO      = 0.05': 'ASK_LO      = 0.30', 'BTC_MOVE_MIN_BPS = 6.0': 'BTC_MOVE_MIN_BPS = 8.0'}),
    ('ask030_move10', {'ASK_LO      = 0.05': 'ASK_LO      = 0.30', 'BTC_MOVE_MIN_BPS = 6.0': 'BTC_MOVE_MIN_BPS = 10.0'}),
    ('ask030_move8_kellyfull', {'ASK_LO      = 0.05': 'ASK_LO      = 0.30', 'BTC_MOVE_MIN_BPS = 6.0': 'BTC_MOVE_MIN_BPS = 8.0', 'KELLY_FRACTION = 0.50': 'KELLY_FRACTION = 1.00'}),
    ('ask030_move8_kellyquarter', {'ASK_LO      = 0.05': 'ASK_LO      = 0.30', 'BTC_MOVE_MIN_BPS = 6.0': 'BTC_MOVE_MIN_BPS = 8.0', 'KELLY_FRACTION = 0.50': 'KELLY_FRACTION = 0.25'}),
    ('ask030_move12', {'ASK_LO      = 0.05': 'ASK_LO      = 0.30', 'BTC_MOVE_MIN_BPS = 6.0': 'BTC_MOVE_MIN_BPS = 12.0'}),
    ('ask030_move8_ttr180', {'ASK_LO      = 0.05': 'ASK_LO      = 0.30', 'BTC_MOVE_MIN_BPS = 6.0': 'BTC_MOVE_MIN_BPS = 8.0', 'ENTRY_TTR_HI = 240.0': 'ENTRY_TTR_HI = 180.0'}),
    ('ask040_move8', {'ASK_LO      = 0.05': 'ASK_LO      = 0.40', 'BTC_MOVE_MIN_BPS = 6.0': 'BTC_MOVE_MIN_BPS = 8.0'}),
    ('ask030_move8_edge020', {'ASK_LO      = 0.05': 'ASK_LO      = 0.30', 'BTC_MOVE_MIN_BPS = 6.0': 'BTC_MOVE_MIN_BPS = 8.0', 'EDGE_MIN    = 0.25': 'EDGE_MIN    = 0.20'}),
    ('ask030_move8_edge030', {'ASK_LO      = 0.05': 'ASK_LO      = 0.30', 'BTC_MOVE_MIN_BPS = 6.0': 'BTC_MOVE_MIN_BPS = 8.0', 'EDGE_MIN    = 0.25': 'EDGE_MIN    = 0.30'}),
]

results = {}
for name, replacements in configs:
    code = base_code
    for old, new in replacements.items():
        if old not in code:
            print(f"WARNING: '{old}' not found for {name}")
        code = code.replace(old, new)
    open('model_submission.py', 'w').write(code)

    result = subprocess.run(
        ['.venv/bin/python3', 'research/eval_battery.py',
         '--model-file', 'model_submission.py', '--label', f'combo_{name}', '--binance-only'],
        capture_output=True, text=True, timeout=300
    )
    out = result.stdout
    pnl_m = re.search(r'total PnL across fixtures\s*:\s*\$\s*([-\d,\.]+)', out)
    win_m = re.search(r'win rate vs baseline\s*:\s*([\d.]+)%', out)
    trades_m = re.search(r'mean trades per fixture\s*:\s*([\d.]+)', out)
    median_m = re.search(r'median primary score\s*:\s*([-\d,\.]+)', out)
    pnl = float(pnl_m.group(1).replace(',','')) if pnl_m else None
    win = float(win_m.group(1)) if win_m else None
    trades = float(trades_m.group(1)) if trades_m else None
    median = float(median_m.group(1).replace(',','')) if median_m else None
    results[name] = (pnl, win, trades, median)
    print(f"{name:28s} | PnL: ${pnl:10,.2f} | Win: {win:5.1f}% | Trades: {trades:4.2f} | Median: {median:8.1f}")

print("\nBest:", max(results.items(), key=lambda x: x[1][0]))
