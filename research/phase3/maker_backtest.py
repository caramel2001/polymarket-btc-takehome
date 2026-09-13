#!/usr/bin/env python3
"""Backtest a two-sided maker over the full-depth tape, with queue-aware fills.

Strategy under test (the rule implied by the Phase-0 markout structure)
----------------------------------------------------------------------
Quote both sides while the crossing flow is benign, and stand aside once it
turns toxic::

    if QUOTE_TTR_LO <= time_to_resolve <= QUOTE_TTR_HI:   post bid and ask
    else:                                                 cancel everything

Phase 0 measured passive markout at **+0.68c** for ttr 120-300s and **−1.62c**
for ttr < 30s, so the withdraw rule is not a free parameter fitted here — it
comes from an independently measured quantity.

Execution is the conservative model in ``polyalpha.passive_sim``: we join the
back of the queue, cancellations are assumed to happen behind us, and we are
only filled when prints actually consume our level.

Every run reports both cost models: ``fee=0`` (real Polymarket, verified from
prints) and the harness's ``0.072*p*(1-p)``.
"""

from __future__ import annotations

import argparse
import glob
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.passive_sim import PassiveSimulator, Side  # noqa: E402


@dataclass(frozen=True)
class Params:
    quote_ttr_lo: float = 120.0     # stand aside inside this (toxic window)
    quote_ttr_hi: float = 300.0
    order_size: float = 100.0       # shares per side
    improve_ticks: int = 0          # 0 = join the touch; 1 = step in front
    tick: float = 0.01
    max_inventory: float = 300.0    # shares net, per token
    requote_move: float = 0.01      # repost if the touch moves this much


def load(tape: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    bf = sorted(glob.glob(str(tape / "books" / "**" / "*.parquet"), recursive=True))
    tf = sorted(glob.glob(str(tape / "trades" / "**" / "*.parquet"), recursive=True))
    if not bf:
        raise SystemExit(f"no book tape under {tape}")
    books = pd.concat([pd.read_parquet(f) for f in bf], ignore_index=True)
    trades = (pd.concat([pd.read_parquet(f) for f in tf], ignore_index=True)
              if tf else pd.DataFrame(columns=["ts", "slug", "token_id", "price",
                                               "size", "taker_side"]))
    return books, trades


def run_event(bk: pd.DataFrame, td: pd.DataFrame, p: Params,
              fee_rate: float) -> dict | None:
    """Replay one event; return its PnL row."""
    outcome = str(bk.resolved_outcome.iloc[-1])
    if outcome not in ("UP", "DOWN"):
        return None
    bk = bk.sort_values("ts")

    # Token identity per side: the trade tape labels by side_token, so we key on
    # that rather than needing the raw token ids here.
    settle = {"UP": 1.0 if outcome == "UP" else 0.0,
              "DOWN": 1.0 if outcome == "DOWN" else 0.0}

    sim = PassiveSimulator(fee_rate=fee_rate)
    live: dict[str, dict[str, int]] = {"UP": {}, "DOWN": {}}   # side -> {BUY/SELL: order_id}
    posted_px: dict[tuple[str, str], float] = {}
    inventory: dict[str, float] = {"UP": 0.0, "DOWN": 0.0}

    td = td.sort_values("ts") if len(td) else td
    t_idx = 0
    t_ts = td.ts.to_numpy() if len(td) else np.array([])

    for row in bk.itertuples(index=False):
        ts = row.ts
        # 1) apply every print that happened up to this book snapshot
        while t_idx < len(t_ts) and t_ts[t_idx] <= ts:
            tr = td.iloc[t_idx]
            fills = sim.on_trade(ts=tr.ts, token_id=tr.side_token, price=tr.price,
                                 size=tr["size"], taker_side=tr.taker_side)
            for f in fills:
                inventory[f.token_id] += f.size if f.side is Side.BUY else -f.size
            t_idx += 1

        ttr = row.time_to_resolve
        in_window = p.quote_ttr_lo <= ttr <= p.quote_ttr_hi

        for side in ("UP", "DOWN"):
            bid = getattr(row, f"{side.lower()}_best_ask") * 0  # placeholder, set below
            bid = getattr(row, f"{side.lower()}_best_bid")
            ask = getattr(row, f"{side.lower()}_best_ask")
            bsz = getattr(row, f"{side.lower()}_best_bid_size")
            asz = getattr(row, f"{side.lower()}_best_ask_size")
            sim.on_book(token_id=side, bid=bid, ask=ask, bid_size=bsz, ask_size=asz)

            if not in_window or bid <= 0 or ask <= 0 or ask <= bid:
                for oid in live[side].values():
                    sim.cancel(oid)
                live[side].clear()
                posted_px.pop((side, "BUY"), None)
                posted_px.pop((side, "SELL"), None)
                continue

            want_bid = round(bid + p.improve_ticks * p.tick, 4)
            want_ask = round(ask - p.improve_ticks * p.tick, 4)
            if want_ask <= want_bid:
                continue

            for tag, want, q in (("BUY", want_bid, bsz), ("SELL", want_ask, asz)):
                # Inventory guard: stop adding to a side we are already long/short.
                inv = inventory[side]
                if tag == "BUY" and inv >= p.max_inventory:
                    continue
                if tag == "SELL" and inv <= -p.max_inventory:
                    continue
                prev = posted_px.get((side, tag))
                oid = live[side].get(tag)
                order = sim._orders.get(oid) if oid else None
                if order is not None and order.is_open and prev is not None \
                        and abs(prev - want) < p.requote_move / 2:
                    continue                       # still at the right price
                if oid:
                    sim.cancel(oid)
                o = sim.post(ts=ts, token_id=side, side=Side(tag), price=want,
                             size=p.order_size, queue_ahead=q)
                live[side][tag] = o.order_id
                posted_px[(side, tag)] = want

    sim.cancel_all()
    r = sim.realised(settle)
    st = sim.stats()
    return {
        "slug": bk.slug.iloc[0], "asset": bk.asset.iloc[0], "horizon": bk.horizon.iloc[0],
        "outcome": outcome, "gross": r["gross"], "fees": r["fees"], "net": r["net"],
        "n_fills": r["n_fills"], "bought": r["shares_bought"], "sold": r["shares_sold"],
        "orders": st["orders_posted"], "fill_rate": st["fill_rate"],
        "inv_up": inventory["UP"], "inv_down": inventory["DOWN"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tape", default=str(ROOT / "data" / "tape"))
    ap.add_argument("--ttr-lo", type=float, default=120.0)
    ap.add_argument("--ttr-hi", type=float, default=300.0)
    ap.add_argument("--size", type=float, default=100.0)
    ap.add_argument("--improve", type=int, default=0)
    ap.add_argument("--out", default=str(ROOT / "research/phase3/maker_backtest.csv"))
    a = ap.parse_args()

    books, trades = load(Path(a.tape))
    p = Params(quote_ttr_lo=a.ttr_lo, quote_ttr_hi=a.ttr_hi,
               order_size=a.size, improve_ticks=a.improve)
    print(f"tape: {books.slug.nunique()} events, {len(trades):,} prints")
    print(f"quote window ttr [{p.quote_ttr_lo:.0f},{p.quote_ttr_hi:.0f}]s  "
          f"size={p.order_size:.0f}  improve={p.improve_ticks} tick(s)\n")

    tby = dict(tuple(trades.groupby("slug"))) if len(trades) else {}
    for label, fee in (("real (fee=0)", 0.0), ("harness (0.072p(1-p))", 0.072)):
        rows = []
        for slug, bk in books.groupby("slug"):
            r = run_event(bk, tby.get(slug, trades.iloc[0:0]), p, fee)
            if r:
                rows.append(r)
        if not rows:
            print("no resolved events")
            return 1
        df = pd.DataFrame(rows)
        net = df.net.to_numpy()
        se = np.std(net, ddof=1) / np.sqrt(len(net)) if len(net) > 1 else np.nan
        print(f"===== {label} =====")
        print(f"  events={len(df)}  net=${net.sum():,.2f}  "
              f"mean=${net.mean():.3f}/event  t={net.mean()/se if se else float('nan'):.2f}")
        print(f"  fills={int(df.n_fills.sum())}  orders={int(df.orders.sum())}  "
              f"fill_rate={df.fill_rate.mean():.1%}  "
              f"shares b/s={df.bought.sum():.0f}/{df.sold.sum():.0f}")
        per_share = net.sum() / max(df.bought.sum() + df.sold.sum(), 1)
        print(f"  net per share traded: {100*per_share:+.3f}c")
        if len(df) > 1:
            byasset = df.groupby("asset").net.agg(["count", "sum", "mean"])
            print(byasset.to_string(float_format=lambda v: f"{v:9.3f}"))
        print()
        if fee == 0.0:
            Path(a.out).parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(a.out, index=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
