#!/usr/bin/env python3
"""Complement pair-maker: sell UP and DOWN together, capture `ask_sum - 1`.

The structure
-------------
UP and DOWN are complements: exactly one settles at $1. So a position that is
short **one UP and one DOWN** pays exactly $1.00 at settlement, whatever happens.
If we sold that pair for more than $1.00, the difference is ours, and it is
**outcome-independent** — no directional risk, and no adverse selection in the
usual sense, because we do not care which side wins.

Measured on the full-depth tape in the quote window (ttr 120-300s):

    ask_sum  mean 1.0191, above 1.00 on 99.9% of ticks   ->  +1.91c per pair

`IDEAS.md` "BATCH OF 6" #1 tested the *opposite* trade — **buying** both sides
when `up_ask + down_ask < 1` — and correctly killed it (the sum is essentially
never below 1; market makers hold it there). The mirror trade, **selling** both
as a maker, was never tested. It is the same no-arbitrage relation read from the
profitable side.

Why the flow makes it fillable
------------------------------
~96% of observed taker prints are BUYs — in a binary market users buy the side
they want rather than selling the other. So resting **asks** get lifted
constantly while resting bids rarely fill. A naive two-sided quoter is wrecked by
that asymmetry (it ends up short one side, naked). Here the same asymmetry is the
enabler: we only ever want to sell, and the flow only ever wants to buy.

The one real risk
-----------------
**Leg risk**: getting lifted on one side and not the other leaves a naked short,
which is a pure directional bet. The strategy's whole job is to keep the legs
matched, so quoting on a side is suspended whenever it runs ahead of the other.
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
    ttr_lo: float = 120.0
    ttr_hi: float = 300.0
    order_size: float = 50.0
    max_leg_imbalance: float = 50.0   # shares one side may run ahead by
    max_pairs: float = 500.0          # cap total pairs per event
    min_edge_c: float = 0.5           # only quote when ask_sum-1 clears this
    requote_move: float = 0.005


def load(tape: Path):
    bf = sorted(glob.glob(str(tape / "books" / "**" / "*.parquet"), recursive=True))
    tf = sorted(glob.glob(str(tape / "trades" / "**" / "*.parquet"), recursive=True))
    books = pd.concat([pd.read_parquet(f) for f in bf], ignore_index=True)
    trades = pd.concat([pd.read_parquet(f) for f in tf], ignore_index=True)
    return books, trades


def run_event(bk: pd.DataFrame, td: pd.DataFrame, p: Params, fee_rate: float):
    outcome = str(bk.resolved_outcome.iloc[-1])
    if outcome not in ("UP", "DOWN"):
        return None
    bk = bk.sort_values("ts")
    settle = {"UP": 1.0 if outcome == "UP" else 0.0,
              "DOWN": 1.0 if outcome == "DOWN" else 0.0}

    sim = PassiveSimulator(fee_rate=fee_rate)
    live: dict[str, int] = {}
    posted: dict[str, float] = {}
    short: dict[str, float] = {"UP": 0.0, "DOWN": 0.0}   # shares sold per token

    td = td.sort_values("ts") if len(td) else td
    t_ts = td.ts.to_numpy() if len(td) else np.array([])
    ti = 0
    ask_sums = []

    for row in bk.itertuples(index=False):
        ts = row.ts
        while ti < len(t_ts) and t_ts[ti] <= ts:
            tr = td.iloc[ti]
            for f in sim.on_trade(ts=tr.ts, token_id=tr.side_token, price=tr.price,
                                  size=tr["size"], taker_side=tr.taker_side):
                if f.side is Side.SELL:
                    short[f.token_id] += f.size
                else:
                    short[f.token_id] -= f.size
            ti += 1

        up_ask, dn_ask = row.up_best_ask, row.down_best_ask
        up_asz, dn_asz = row.up_best_ask_size, row.down_best_ask_size
        sim.on_book(token_id="UP", bid=row.up_best_bid, ask=up_ask,
                    bid_size=row.up_best_bid_size, ask_size=up_asz)
        sim.on_book(token_id="DOWN", bid=row.down_best_bid, ask=dn_ask,
                    bid_size=row.down_best_bid_size, ask_size=dn_asz)

        ask_sum = up_ask + dn_ask
        pairs = min(short["UP"], short["DOWN"])
        in_window = p.ttr_lo <= row.time_to_resolve <= p.ttr_hi
        edge_ok = (ask_sum - 1.0) * 100 >= p.min_edge_c
        tradable = up_ask > 0 and dn_ask > 0 and up_ask < 1 and dn_ask < 1

        if not (in_window and edge_ok and tradable) or pairs >= p.max_pairs:
            for oid in live.values():
                sim.cancel(oid)
            live.clear(); posted.clear()
            continue
        ask_sums.append(ask_sum)

        for side, px, q in (("UP", up_ask, up_asz), ("DOWN", dn_ask, dn_asz)):
            other = "DOWN" if side == "UP" else "UP"
            # Leg discipline: do not let one side run ahead of the other.
            if short[side] - short[other] >= p.max_leg_imbalance:
                if side in live:
                    sim.cancel(live.pop(side)); posted.pop(side, None)
                continue
            want = round(px, 4)
            oid = live.get(side)
            o = sim._orders.get(oid) if oid else None
            if o is not None and o.is_open and posted.get(side) is not None \
                    and abs(posted[side] - want) < p.requote_move:
                continue
            if oid:
                sim.cancel(oid)
            no = sim.post(ts=ts, token_id=side, side=Side.SELL, price=want,
                          size=p.order_size, queue_ahead=q)
            live[side] = no.order_id
            posted[side] = want

    sim.cancel_all()
    r = sim.realised(settle)
    st = sim.stats()
    pairs = min(short["UP"], short["DOWN"])
    return {
        "slug": bk.slug.iloc[0], "asset": bk.asset.iloc[0], "horizon": bk.horizon.iloc[0],
        "outcome": outcome, "net": r["net"], "gross": r["gross"], "fees": r["fees"],
        "n_fills": r["n_fills"], "sold_up": short["UP"], "sold_down": short["DOWN"],
        "pairs": pairs, "naked": abs(short["UP"] - short["DOWN"]),
        "fill_rate": st["fill_rate"], "orders": st["orders_posted"],
        "mean_ask_sum": float(np.mean(ask_sums)) if ask_sums else np.nan,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tape", default=str(ROOT / "data" / "tape"))
    ap.add_argument("--ttr-lo", type=float, default=120.0)
    ap.add_argument("--ttr-hi", type=float, default=300.0)
    ap.add_argument("--size", type=float, default=50.0)
    ap.add_argument("--max-imbalance", type=float, default=50.0)
    ap.add_argument("--min-edge-c", type=float, default=0.5)
    ap.add_argument("--out", default=str(ROOT / "research/phase3/pair_maker.csv"))
    a = ap.parse_args()

    books, trades = load(Path(a.tape))
    p = Params(ttr_lo=a.ttr_lo, ttr_hi=a.ttr_hi, order_size=a.size,
               max_leg_imbalance=a.max_imbalance, min_edge_c=a.min_edge_c)
    print(f"tape: {books.slug.nunique()} events, {len(trades):,} prints")
    print(f"window ttr[{p.ttr_lo:.0f},{p.ttr_hi:.0f}]  size={p.order_size:.0f}  "
          f"max_leg_imbalance={p.max_leg_imbalance:.0f}  min_edge={p.min_edge_c}c\n")

    tby = dict(tuple(trades.groupby("slug")))
    for label, fee in (("real (fee=0)", 0.0), ("harness (0.072p(1-p))", 0.072)):
        rows = [r for slug, bk in books.groupby("slug")
                if (r := run_event(bk, tby.get(slug, trades.iloc[0:0]), p, fee))]
        if not rows:
            print("no resolved events"); return 1
        df = pd.DataFrame(rows)
        net = df.net.to_numpy()
        se = np.std(net, ddof=1)/np.sqrt(len(net)) if len(net) > 1 else np.nan
        print(f"===== {label} =====")
        print(f"  events={len(df)}  net=${net.sum():,.2f}  mean=${net.mean():+.3f}/event  "
              f"t={net.mean()/se if se else float('nan'):+.2f}")
        print(f"  events profitable: {(net>0).sum()}/{len(net)} ({(net>0).mean():.0%})")
        print(f"  pairs={df.pairs.sum():.0f}  naked={df.naked.sum():.0f}  "
              f"fills={int(df.n_fills.sum())}  fill_rate={df.fill_rate.mean():.1%}")
        if df.pairs.sum() > 0:
            print(f"  net per pair: {100*net.sum()/df.pairs.sum():+.3f}c  "
                  f"(structural edge ~{100*(df.mean_ask_sum.mean()-1):+.2f}c)")
        print(df.groupby("asset").net.agg(["count","sum","mean"]).to_string(
            float_format=lambda v: f"{v:9.3f}"))
        print()
        if fee == 0.0:
            Path(a.out).parent.mkdir(parents=True, exist_ok=True); df.to_csv(a.out, index=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
