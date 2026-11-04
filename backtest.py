#!/usr/bin/env python3
"""
Backtest: replays the strategy on past 1-hour candles (about 8 months) for every
risk level, both WITH the smart filters and WITHOUT them ("basic"), so you can see
whether the filters actually help.

It also checks which trading sessions, weekdays and pairs worked best. To avoid fooling
ourselves, it "learns" on the first ~5 months (practice) and checks on the last ~3 months
the strategy has never seen (final exam).

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
                entry = S.from_iso(s["created"])
                trades.append({"pair": pair, "status": s["status"], "r": round(r, 3),
                               "score": s["score"], "closed": s["closed_at"], "created": s["created"],
                               "session": session_name(entry.hour), "weekday": entry.strftime("%a")})
                open_sig = None
            continue
        sig, _ = S.evaluate(pair, ind, i, profile, use_filters)
        if sig:
            open_sig = sig
    return trades


TRAIN_SHARE = 0.62     # first ~5 months = "practice", last ~3 months = "final exam"
MIN_TRADES = 15        # a session/pair needs at least this many practice trades to be judged
SESSIONS = [("Asian", 0, 7), ("London", 7, 12), ("London + New York", 12, 16), ("New York", 16, 21), ("Late / rollover", 21, 24)]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sun"]


def session_name(hour_utc):
    for name, a, b in SESSIONS:
        if a <= hour_utc < b:
            return name
    return "Late / rollover"


def mini(trades):
    st = stats(trades)
    return {k: st[k] for k in ("trades", "win_rate", "net_r", "avg_r")}


def breakdown(train, test, key, names):
    return [{"name": n, "practice": mini([t for t in train if t[key] == n]),
             "exam": mini([t for t in test if t[key] == n])} for n in names]


def learn_filter(train, key, names):
    """Keep only groups that made money in the practice months (with enough trades)."""
    keep = []
    for n in names:
        g = [t for t in train if t[key] == n]
        if len(g) >= MIN_TRADES and sum(t["r"] for t in g) > 0:
            keep.append(n)
    return keep


def time_analysis(trades, split_time):
    train = [t for t in trades if t["created"] < split_time]
    test = [t for t in trades if t["created"] >= split_time]
    sess = [n for n, _, _ in SESSIONS]
    pairs = list(S.PAIRS)
    ok_sess = learn_filter(train, "session", sess)
    ok_pairs = learn_filter(train, "pair", pairs)
    keep = lambda ts: [t for t in ts if t["session"] in ok_sess and t["pair"] in ok_pairs]
    exam_all, exam_kept = stats(test), stats(keep(test))
    helps = (exam_kept["trades"] >= 10 and exam_kept["avg_r"] is not None and exam_all["avg_r"] is not None
             and exam_kept["avg_r"] > exam_all["avg_r"])
    profitable = helps and exam_kept["net_r"] > 0
    if not ok_sess or not ok_pairs:
        verdict = (f"Nothing to keep: no session or pair had at least {MIN_TRADES} practice trades AND made money. "
                   "There is no clear time/pair edge here.")
    elif profitable:
        verdict = "Passed the final exam: on months it had never seen, the time/pair filter did better AND made money (before spreads). Worth trying on a demo account."
    elif helps:
        verdict = "Partly helped: on unseen months it lost less than trading every session/pair, but it still did not make money."
    else:
        verdict = "Did not pass: on unseen months the filter was not better. The good practice results were probably luck."
    if (ok_sess and ok_pairs) and exam_kept["trades"] < 30:
        verdict += f" Caution: only {exam_kept['trades']} exam trades, so this could still be luck."
    return {
        "split": split_time,
        "practice": mini(train), "exam": mini(test),
        "by_session": breakdown(train, test, "session", sess),
        "by_weekday": breakdown(train, test, "weekday", WEEKDAYS),
        "by_pair": breakdown(train, test, "pair", pairs),
        "filter": {"sessions": ok_sess, "pairs": ok_pairs,
                   "practice_kept": mini(keep(train)), "exam_all": mini(test), "exam_kept": mini(keep(test)),
                   "helps": helps, "profitable": profitable, "verdict": verdict},
    }


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
        split_i = WARMUP + int((len(closed) - WARMUP) * TRAIN_SHARE)
        ranges[pair] = {"from": S.iso(closed[WARMUP]["time"]), "to": S.iso(closed[-1]["time"]),
                        "split": S.iso(closed[split_i]["time"]), "candles": len(closed) - WARMUP}
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
        split = min((r["split"] for r in ranges.values()), default=S.iso(now))
        entry["time_analysis"] = time_analysis(per[prof]["filtered"], split)
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
        ta = e["time_analysis"]["filter"]
        print(f"{'':9s} TIME FILTER keep sessions={ta['sessions']} pairs={ta['pairs']} | final exam: "
              f"all={ta['exam_all']['net_r']}R ({ta['exam_all']['trades']} trades) -> kept={ta['exam_kept']['net_r']}R "
              f"({ta['exam_kept']['trades']} trades) | {ta['verdict']}")
    return result


if __name__ == "__main__":
    if not os.getenv("TWELVEDATA_API_KEY"):
        sys.exit("Missing TWELVEDATA_API_KEY secret.")
    run()
