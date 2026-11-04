#!/usr/bin/env python3
"""
Backtest: replays the strategy on past 1-hour candles (about 8 months) for every
risk level, both WITH the smart filters and WITHOUT them ("basic"), so you can see
whether the filters actually help.

Results are saved to docs/data/backtest.json and shown on the website.

Limits (be aware):
  * Spreads, commissions and slippage are NOT included -> real results will be a bit worse.
  * The news filter is NOT included (no free history of past news).
  * Good past results do not guarantee future results.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import signals as S

HISTORY = 5000          # 1-hour candles per pair (Twelve Data free plan maximum)
WARMUP = S.EMA_SLOW + S.ATR_AVG_LEN + 20
RISK_USD = 3.0          # example: risking $3 per trade (1% of a $300 account)
OUT_FILE = os.path.join(S.ROOT, "docs", "data", "backtest.json")


def simulate(pair, ind, profile, use_filters):
    """Walk through history candle by candle. One open trade at a time."""
    candles = ind["candles"]
    trades, open_sig = [], None
    p = S.PROFILES[profile]
    rr = p["tp_atr"] / p["sl_atr"]
    for i in range(WARMUP, len(candles)):
        c = candles[i]
        if open_sig:
            close_time = c["time"] + timedelta(hours=S.INTERVAL_HOURS)
            if S.update_open_signal(open_sig, [c], close_time):
                s = open_sig
                if s["status"] == "win":
                    r = rr
                elif s["status"] == "loss":
                    r = -1.0
                else:
                    risk = abs(s["entry"] - s["sl"]) or 1e-9
                    move = (s["exit_price"] - s["entry"]) if s["direction"] == "BUY" else (s["entry"] - s["exit_price"])
                    r = move / risk
                trades.append({"pair": pair, "status": s["status"], "r": round(r, 3),
                               "score": s["score"], "closed": s["closed_at"], "created": s["created"]})
                open_sig = None
            continue
        sig, _ = S.evaluate(pair, ind, i, profile, use_filters)
        if sig:
            open_sig = sig
    return trades


def stats(trades):
    w = sum(1 for t in trades if t["status"] == "win")
    l = sum(1 for t in trades if t["status"] == "loss")
    e = sum(1 for t in trades if t["status"] == "expired")
    rs = [t["r"] for t in sorted(trades, key=lambda t: t["closed"])]
    net = sum(rs)
    pos, neg = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
    streak = worst = 0
    eq = peak = dd = 0.0
    for r in rs:
        streak = streak + 1 if r < 0 else 0
        worst = max(worst, streak)
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {
        "trades": len(trades), "wins": w, "losses": l, "expired": e,
        "win_rate": round(100 * w / (w + l), 1) if w + l else None,
        "net_r": round(net, 2),
        "avg_r": round(net / len(rs), 3) if rs else None,
        "profit_factor": round(pos / neg, 2) if neg else None,
        "worst_losing_streak": worst,
        "max_drawdown_r": round(dd, 2),
        "net_usd": round(net * RISK_USD, 2),
        "max_drawdown_usd": round(dd * RISK_USD, 2),
    }


def equity(trades, points=120):
    rs = sorted(trades, key=lambda t: t["closed"])
    curve, eq = [], 0.0
    for t in rs:
        eq += t["r"]
        curve.append(round(eq, 2))
    if len(curve) > points:
        step = len(curve) / points
        curve = [curve[int(k * step)] for k in range(points - 1)] + [curve[-1]]
    return curve


def score_buckets(trades):
    out = []
    for name, lo, hi in (("Below 50", 0, 49), ("50 to 69", 50, 69), ("70 and above", 70, 100)):
        b = [t for t in trades if lo <= t["score"] <= hi]
        st = stats(b)
        out.append({"band": name, "trades": st["trades"], "win_rate": st["win_rate"], "avg_r": st["avg_r"]})
    return out


def run(fetch=None):
    fetch = fetch or (lambda pair, key: S.fetch_candles(pair, key, HISTORY))
    key = os.getenv("TWELVEDATA_API_KEY", "")
    now = datetime.now(timezone.utc)
    per = {p: {"filtered": [], "basic": []} for p in S.PROFILES}
    ranges = {}
    for n, pair in enumerate(S.PAIRS):
        if n:
            time.sleep(10)   # free plan: max 8 requests per minute
        try:
            candles = fetch(pair, key)
        except Exception as e:
            print(f"[{pair}] could not get prices: {e}")
            continue
        closed = [c for c in candles if c["time"] + timedelta(hours=S.INTERVAL_HOURS) <= now] or candles
        if len(closed) < WARMUP + 100:
            print(f"[{pair}] not enough history ({len(closed)} candles)")
            continue
        ranges[pair] = {"from": S.iso(closed[WARMUP]["time"]), "to": S.iso(closed[-1]["time"]),
                        "candles": len(closed) - WARMUP}
        ind = S.indicators(closed)
        for prof in S.PROFILES:
            for variant, flt in (("filtered", True), ("basic", False)):
                t = simulate(pair, ind, prof, flt)
                per[prof][variant] += t
                print(f"[{pair}][{prof}][{variant}] {len(t)} trades")

    result = {"generated": S.iso(now), "risk_usd_example": RISK_USD, "ranges": ranges,
              "notes": ["Spreads, commissions and slippage are not included, so real results will be a bit worse.",
                        "The Forex Factory news filter is not included (no free history of past news).",
                        "Past results do not guarantee future results."],
              "profiles": {}}
    all_basic = []
    for prof in S.PROFILES:
        entry = {}
        for variant in ("filtered", "basic"):
            tr = per[prof][variant]
            entry[variant] = stats(tr)
            entry[variant]["by_pair"] = {pair: stats([t for t in tr if t["pair"] == pair]) for pair in S.PAIRS}
            entry[variant]["equity"] = equity(tr)
        entry["score_buckets"] = score_buckets(per[prof]["basic"])
        result["profiles"][prof] = entry
        all_basic += per[prof]["basic"]
    result["score_buckets_all"] = score_buckets(all_basic)

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
        f.write("\n")
    for prof, e in result["profiles"].items():
        f_, b_ = e["filtered"], e["basic"]
        print(f"{prof:9s} FILTERED trades={f_['trades']} win%={f_['win_rate']} netR={f_['net_r']} | "
              f"BASIC trades={b_['trades']} win%={b_['win_rate']} netR={b_['net_r']}")
    return result


if __name__ == "__main__":
    if not os.getenv("TWELVEDATA_API_KEY"):
        sys.exit("Missing TWELVEDATA_API_KEY secret.")
    run()
