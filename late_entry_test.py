#!/usr/bin/env python3
"""
One-off experiment: what happens if you enter a signal LATE, after the price has
already moved against it?

For every 4-hour signal the backtest finds (same 2.6 years, same rules, spreads included),
we follow the 4-hour candles after the alert and compare:
  * ON TIME : enter at the signal's entry price (what the backtest normally assumes)
  * LATE X  : wait until the price has moved X of the stop distance AGAINST the trade
              (e.g. 0.3 = 30% of the way to the stop loss), then enter there,
              keeping the signal's original TP and SL (like entering gold at 3650).
If the price never moves that far against the trade before TP/SL, there is no late entry.

Money is measured with the SAME lot size for every trade, in "R" = the money the
ON-TIME trade would risk. So a late loss costs less than 1R (stop is closer) and a late
win pays more than the on-time win (TP is further away).

If the late-entry price and the stop are touched in the same candle we can't know the
order, so it counts as a LOSS (cautious). Same if TP and SL are touched in one candle.

Results are printed to the log and saved to docs/data/late_entry_test.json.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

os.environ.setdefault("INTERVAL_HOURS", "4")
import signals as S
import backtest as B

LEVELS = [0.25, 0.5, 0.75]
PROFS = ["low", "moderate", "focused", "high"]
OUT = os.path.join(S.ROOT, "docs/data/late_entry_test.json")


def follow(direction, pair, entry, tp, sl, h1, start_idx, first_check_tp=True):
    """Walk candles from start_idx. Returns ('win'|'loss'|'expired', exit_price, idx)."""
    half = S.SPREAD_PIPS.get(pair, 0) * S.pip_size(pair) / 2
    t0 = h1[start_idx]["time"]
    for k in range(start_idx, len(h1)):
        c = h1[k]
        if direction == "BUY":
            hit_sl, hit_tp = c["low"] - half <= sl, c["high"] - half >= tp
        else:
            hit_sl, hit_tp = c["high"] + half >= sl, c["low"] + half <= tp
        if k == start_idx and not first_check_tp:
            hit_tp = False       # entry candle: we can't know if TP came before our entry
        if hit_sl:
            return "loss", sl, k
        if hit_tp:
            return "win", tp, k
        if c["time"] - t0 > timedelta(hours=360):
            return "expired", c["close"], k
    return "open", h1[-1]["close"], len(h1) - 1


def run_pair(pair, h1, prof):
    ind = S.indicators(h1)
    candles = ind["candles"]
    out, i, busy_until = [], B.WARMUP, None
    for i in range(B.WARMUP, len(candles)):
        close_t = candles[i]["time"] + timedelta(hours=S.INTERVAL_HOURS)
        if busy_until and close_t <= busy_until:
            continue                                  # one trade at a time, like the bot
        sig, _ = S.evaluate(pair, ind, i, prof, True)
        if not sig:
            continue
        created = S.from_iso(sig["created"])
        start = i + 1
        if start >= len(h1):
            break
        d, e, tp, sl = sig["direction"], sig["entry"], sig["tp"], sig["sl"]
        risk = abs(e - sl)
        res, px, k_end = follow(d, pair, e, tp, sl, h1, start)
        if res == "open":
            break
        busy_until = h1[k_end]["time"] + timedelta(hours=S.INTERVAL_HOURS)
        on_r = (abs(tp - e) / risk if res == "win" else -1.0 if res == "loss"
                else ((px - e) if d == "BUY" else (e - px)) / risk)
        row = {"pair": pair, "time": sig["created"], "dir": d, "on_time": res, "on_r": round(on_r, 3)}
        for X in LEVELS:
            trig = e - X * risk if d == "BUY" else e + X * risk
            entered = None
            last = k_end if res == "loss" else k_end - 1   # only while the original trade is still alive
            for k in range(start, last + 1):
                c = h1[k]
                if (d == "BUY" and c["low"] <= trig) or (d == "SELL" and c["high"] >= trig):
                    entered = k
                    break
            if entered is None:
                row[str(X)] = None
                continue
            r2, px2, _ = follow(d, pair, trig, tp, sl, h1, entered, first_check_tp=False)
            if r2 == "win":
                late_r = abs(tp - trig) / risk
            elif r2 == "loss":
                late_r = -abs(trig - sl) / risk
            else:
                late_r = ((px2 - trig) if d == "BUY" else (trig - px2)) / risk
            row[str(X)] = {"res": r2, "r": round(late_r, 3)}
        out.append(row)
    return out


def summ(rows, key):
    if key == "on":
        rs = [(r["on_time"], r["on_r"]) for r in rows]
    else:
        rs = [(r[key]["res"], r[key]["r"]) for r in rows if r.get(key)]
    w = sum(1 for s, _ in rs if s == "win")
    l = sum(1 for s, _ in rs if s == "loss")
    net = sum(x for _, x in rs)
    return {"trades": len(rs), "wins": w, "losses": l,
            "win_rate": round(100 * w / (w + l), 1) if w + l else None,
            "net_r": round(net, 1), "avg_r": round(net / len(rs), 3) if rs else None}


def main():
    key = os.getenv("TWELVEDATA_API_KEY", "")
    now = datetime.now(timezone.utc)
    data, result = {}, {"generated": S.iso(now), "levels": LEVELS, "profiles": {}}
    for n, pair in enumerate(S.PAIRS):
        if n:
            time.sleep(10)
        h1 = S.fetch_candles(pair, key, B.HISTORY)
        data[pair] = [c for c in h1 if c["time"] + timedelta(hours=S.INTERVAL_HOURS) <= now]
        print(f"[{pair}] {len(data[pair])} candles {data[pair][0]['time']} -> {data[pair][-1]['time']}")
    for prof in PROFS:
        rows = []
        for pair, h1 in data.items():
            rows += run_pair(pair, h1, prof)
        res = {"all": {"on_time": summ(rows, "on")}, "gold": {}}
        gold = [r for r in rows if r["pair"] == "XAU/USD"]
        res["gold"]["on_time"] = summ(gold, "on")
        for X in LEVELS:
            k = str(X)
            res["all"][f"late_{k}"] = summ(rows, k)
            res["gold"][f"late_{k}"] = summ(gold, k)
            # the same signals, entered on time (to compare like with like)
            res["all"][f"same_signals_on_time_{k}"] = summ([r for r in rows if r.get(k)], "on")
            res["gold"][f"same_signals_on_time_{k}"] = summ([r for r in gold if r.get(k)], "on")
        result["profiles"][prof] = res
        print(f"\n=== {prof.upper()} ===")
        for scope in ("all", "gold"):
            for name, s in res[scope].items():
                print(f"  {scope:4s} {name:28s} trades={s['trades']:3d} win%={s['win_rate']} netR={s['net_r']} avgR={s['avg_r']}")
    with open(OUT, "w") as f:
        json.dump(result, f, indent=2)


if __name__ == "__main__":
    if not os.getenv("TWELVEDATA_API_KEY"):
        sys.exit("Missing TWELVEDATA_API_KEY secret.")
    main()
