#!/usr/bin/env python3
"""
Forex signal bot with three risk levels (Low / Moderate / High),
smart filters and a confidence score.

Runs on GitHub Actions every 15 minutes:
  1. Downloads recent 1-hour candles for each pair (Twelve Data, free plan).
  2. Reads the Forex Factory economic calendar (high/medium impact news).
  3. For each risk level: checks open suggestions (TP hit = win, SL hit = loss),
     warns about big news for open trades, and looks for new setups.
     Every setup gets a confidence score (0-100) with reasons for and against.
  4. Sends alerts (Telegram and/or Gmail).
  5. Saves everything to docs/data/signals.json (shown on the website),
     including an hourly "market snapshot" used by the Claude check-ins.

This is a rule-based educational tool, not financial advice.
"""
import json
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

import requests

# ----------------------------------------------------------------------------
# Settings (safe to change)
# ----------------------------------------------------------------------------
PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY", "XAU/USD"]
INTERVAL_HOURS = 1          # 1-hour candles (shared by all risk levels)
CANDLES = 1000              # candles to download each run (also builds the 4-hour chart)
EMA_FAST, EMA_SLOW = 50, 200        # 1-hour trend
H4_FAST, H4_SLOW = 20, 50           # 4-hour (bigger picture) trend
RSI_LEN = 14
ATR_LEN = 14
ADX_LEN = 14                # trend strength
ATR_AVG_LEN = 100           # "normal" volatility, to spot unusually quiet or wild markets
SWING_BARS = 48             # look back 48 hours for recent highs / lows
EXPIRE_HOURS = 120          # close a suggestion as "expired" after 5 days with no TP/SL
NEWS_WARN_MIN = 60          # warn about open trades when high-impact news is within 60 min
NEWS_ALERT_HOURS = 24       # list high-impact news for the next 24h inside each new alert
SNAPSHOT_EVERY_MIN = 55     # save a market snapshot about once an hour
MY_TIMEZONE = timezone(timedelta(hours=8))   # Malaysia time for alert messages

# Typical broker spread (in pips). Prices from Twelve Data are MID prices, but a broker closes a
# BUY at the lower "bid" price and a SELL at the higher "ask" price. Using half the spread when
# checking TP/SL makes the bot (and the backtest) judge trades the way the broker does.
SPREAD_PIPS = {"EUR/USD": 1.0, "GBP/USD": 1.5, "USD/JPY": 1.5, "XAU/USD": 3.0}

# Which risk levels send Telegram/Gmail alerts. All levels are always recorded on the website.
# High risk alerts are ON at the user's request (for demo practice), with a clear warning,
# because the backtest showed it losing clearly (about -81R over ~8 months).
ALERT_PROFILES = ["low", "moderate", "high", "focused"]
ALERT_WARNINGS = {"high": "⚠️ Backtest: this level LOST over ~8 months (about -81R). Demo practice only."}

PROFILES = {
    "low": {
        "label": "Low risk", "icon": "🛡️",
        "rsi_buy": 35, "rsi_sell": 65,      # needs a deeper pullback
        "sl_atr": 2.0, "tp_atr": 2.0,       # wide SL, TP same size as SL (1 : 1)
        "trend_slope": True,                # long-term trend must also be rising/falling
        "price_filter": True,               # price must be on the right side of EMA200
        "h4_rule": "agree",                 # 4-hour trend MUST point the same way
        "min_adx": 20,                      # trend must have some strength
        "min_score": 70,                    # only high-confidence setups
        "news_before": 120, "news_after": 60, "news_medium": True,
        "desc": "Very picky: only strong, clear trends where the 4-hour chart agrees, after a deep "
                "pullback, with a confidence score of 70+. Wide stop loss and TP equal to SL. "
                "Pauses 2h before to 1h after high AND medium impact news.",
    },
    "moderate": {
        "label": "Moderate", "icon": "⚖️",
        "rsi_buy": 40, "rsi_sell": 60,
        "sl_atr": 1.5, "tp_atr": 2.25,      # reward : risk = 1.5 : 1
        "trend_slope": False,
        "price_filter": True,
        "h4_rule": "not_against",           # 4-hour trend must not point the other way
        "min_adx": 0,
        "min_score": 55,
        "news_before": 60, "news_after": 30, "news_medium": False,
        "desc": "Balanced: trend + normal pullback, 4-hour trend not against it, confidence 55+. "
                "TP is 1.5x the SL. Pauses 1h before to 30 min after high impact news.",
    },
    "high": {
        "label": "High risk", "icon": "🔥",
        "rsi_buy": 45, "rsi_sell": 55,      # small pullbacks count -> more trades
        "sl_atr": 1.0, "tp_atr": 2.0,       # tight SL, TP = 2x SL (2 : 1)
        "trend_slope": False,
        "price_filter": False,
        "h4_rule": None,
        "min_adx": 0,
        "min_score": 40,
        "news_before": 15, "news_after": 15, "news_medium": False,
        "desc": "Least picky: more trades, tight stop loss, TP is 2x the SL, confidence 40+. "
                "Expect more losing trades but bigger wins. Only pauses 15 min around high impact news. "
                "Alerts are ON for demo practice only: the backtest showed this level losing clearly.",
    },
    "focused": {
        "label": "Focused", "icon": "🎯",
        "rsi_buy": 40, "rsi_sell": 60,      # same rules as Moderate...
        "sl_atr": 1.5, "tp_atr": 2.25,
        "trend_slope": False,
        "price_filter": True,
        "h4_rule": "not_against",
        "min_adx": 0,
        "min_score": 55,
        "news_before": 60, "news_after": 30, "news_medium": False,
        "only_pairs": ["GBP/USD", "XAU/USD"],   # ...but only these pairs
        "hours_utc": [0, 12],                    # ...and only Asian + London sessions (8am-8pm Malaysia time)
        "desc": "Moderate rules, but only GBP/USD and gold, and only during the Asian + London sessions "
                "(8am-8pm Malaysia time). Picked from the backtest's practice months; it then made +4R on "
                "unseen months, but from only 21 trades, so it could still be luck. Demo-test it first.",
    },
}

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(ROOT, "docs", "data", "signals.json")
API_URL = "https://api.twelvedata.com/time_series"
NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"   # Forex Factory calendar feed


# ----------------------------------------------------------------------------
# Indicators (all only look at past candles, so they are safe for backtests)
# ----------------------------------------------------------------------------
def ema(values, n):
    out = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def sma(values, n):
    out = [None] * len(values)
    s, cnt, q = 0.0, 0, []
    for i, v in enumerate(values):
        if v is None:
            continue
        q.append(v)
        s += v
        if len(q) > n:
            s -= q.pop(0)
        if len(q) == n:
            out[i] = s / n
    return out


def rsi(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0)
        losses += max(-ch, 0)
    avg_g, avg_l = gains / n, losses / n
    out[n] = 100 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(ch, 0)) / n
        avg_l = (avg_l * (n - 1) + max(-ch, 0)) / n
        out[i] = 100 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def true_ranges(highs, lows, closes):
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    return trs


def atr(highs, lows, closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    trs = true_ranges(highs, lows, closes)
    prev = sum(trs[1:n + 1]) / n
    out[n] = prev
    for i in range(n + 1, len(closes)):
        prev = (prev * (n - 1) + trs[i]) / n
        out[i] = prev
    return out


def adx(highs, lows, closes, n=14):
    """Average Directional Index: trend strength (under 20 = weak/choppy, over 25 = strong)."""
    L = len(closes)
    out = [None] * L
    if L < 2 * n + 1:
        return out
    trs = true_ranges(highs, lows, closes)
    pdm, mdm = [0.0] * L, [0.0] * L
    for i in range(1, L):
        up, dn = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
    tr_s, p_s, m_s = sum(trs[1:n + 1]), sum(pdm[1:n + 1]), sum(mdm[1:n + 1])
    dxs, a = [], None
    for i in range(n, L):
        if i > n:
            tr_s += trs[i] - tr_s / n
            p_s += pdm[i] - p_s / n
            m_s += mdm[i] - m_s / n
        pdi = 100 * p_s / tr_s if tr_s else 0
        mdi = 100 * m_s / tr_s if tr_s else 0
        dx = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0
        dxs.append(dx)
        if len(dxs) == n:
            a = sum(dxs) / n
            out[i] = a
        elif len(dxs) > n:
            a = (a * (n - 1) + dx) / n
            out[i] = a
    return out


def to_h4(closed):
    """Group 1-hour candles into 4-hour candles (00, 04, 08, 12, 16, 20 UTC)."""
    out = []
    for c in closed:
        t = c["time"]
        key = t.replace(hour=t.hour - t.hour % 4, minute=0, second=0, microsecond=0)
        if out and out[-1]["time"] == key:
            g = out[-1]
            g["high"] = max(g["high"], c["high"])
            g["low"] = min(g["low"], c["low"])
            g["close"] = c["close"]
            g["last"] = t
        else:
            out.append({"time": key, "open": c["open"], "high": c["high"], "low": c["low"],
                        "close": c["close"], "last": t})
    return out


def indicators(closed):
    """Everything the strategy needs, computed once for the whole candle list."""
    closes = [c["close"] for c in closed]
    highs = [c["high"] for c in closed]
    lows = [c["low"] for c in closed]
    a = atr(highs, lows, closes, ATR_LEN)
    h4 = to_h4(closed)
    h4c = [g["close"] for g in h4]
    h4f, h4s = ema(h4c, H4_FAST), ema(h4c, H4_SLOW)
    # For each 1H candle, the latest 4H candle that had fully finished by then
    h4idx, j = [], -1
    for c in closed:
        close_time = c["time"] + timedelta(hours=INTERVAL_HOURS)
        while j + 1 < len(h4) and h4[j + 1]["time"] + timedelta(hours=4) <= close_time:
            j += 1
        h4idx.append(j)
    return {
        "candles": closed, "close": closes, "high": highs, "low": lows,
        "ef": ema(closes, EMA_FAST), "es": ema(closes, EMA_SLOW),
        "rsi": rsi(closes, RSI_LEN), "atr": a, "atr_avg": sma(a, ATR_AVG_LEN),
        "adx": adx(highs, lows, closes, ADX_LEN),
        "h4": h4, "h4c": h4c, "h4f": h4f, "h4s": h4s, "h4idx": h4idx,
    }


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def decimals(pair):
    return 3 if "JPY" in pair else 2 if "XAU" in pair else 5


def pip_size(pair):
    return 0.01 if "JPY" in pair else 0.1 if "XAU" in pair else 0.0001


def parse_time(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def my_time(dt):
    return dt.astimezone(MY_TIMEZONE).strftime("%a %d %b, %I:%M %p (MYT)")


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"signals": []}
    data.setdefault("signals", [])
    for s in data["signals"]:
        s.setdefault("profile", "moderate")      # older suggestions = moderate
    return data


def save_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def strategy_info():
    return {
        "name": "Trend + RSI pullback + smart filters",
        "timeframe": f"{INTERVAL_HOURS}H",
        "pairs": PAIRS,
        "profiles": {
            k: {"label": p["label"], "icon": p["icon"], "desc": p["desc"],
                "reward_risk": round(p["tp_atr"] / p["sl_atr"], 2),
                "sl_atr": p["sl_atr"], "tp_atr": p["tp_atr"],
                "rsi_buy": p["rsi_buy"], "rsi_sell": p["rsi_sell"],
                "min_score": p["min_score"], "h4_rule": p["h4_rule"], "min_adx": p["min_adx"],
                "only_pairs": p.get("only_pairs"), "hours_utc": p.get("hours_utc"),
                "alerts": k in ALERT_PROFILES}
            for k, p in PROFILES.items()
        },
    }


# ----------------------------------------------------------------------------
# Price data
# ----------------------------------------------------------------------------
def fetch_candles(pair, api_key, size=CANDLES):
    params = {"symbol": pair, "interval": f"{INTERVAL_HOURS}h", "outputsize": size,
              "timezone": "UTC", "apikey": api_key}
    r = requests.get(API_URL, params=params, timeout=60)
    r.raise_for_status()
    js = r.json()
    if js.get("status") != "ok" or "values" not in js:
        raise RuntimeError(js.get("message", "unknown error from Twelve Data"))
    candles = [{"time": parse_time(v["datetime"]), "open": float(v["open"]),
                "high": float(v["high"]), "low": float(v["low"]), "close": float(v["close"])}
               for v in js["values"]]
    candles.sort(key=lambda c: c["time"])
    return candles


# ----------------------------------------------------------------------------
# News (Forex Factory calendar feed)
# ----------------------------------------------------------------------------
def fetch_news():
    r = requests.get(NEWS_URL, timeout=20, headers={"User-Agent": "forex-signals-bot"})
    r.raise_for_status()
    events = []
    for e in r.json():
        try:
            t = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
        except Exception:
            continue
        events.append({"time": iso(t), "currency": e.get("country", ""),
                       "impact": e.get("impact", ""), "title": e.get("title", ""),
                       "forecast": e.get("forecast", ""), "previous": e.get("previous", "")})
    events.sort(key=lambda x: x["time"])
    return events


def pair_currencies(pair):
    base, quote = pair.split("/")
    return {"USD"} if base == "XAU" else {base, quote}   # gold mostly reacts to US news


def news_for(pair, events, now, start_min, end_min, impacts=("High",)):
    """Events for this pair's currencies between now+start_min and now+end_min."""
    cur = pair_currencies(pair)
    out = []
    for e in events or []:
        if e["impact"] not in impacts or e["currency"] not in cur:
            continue
        mins = (from_iso(e["time"]) - now).total_seconds() / 60
        if start_min <= mins <= end_min:
            out.append(e)
    return out


def news_line(e):
    return f"• {my_time(from_iso(e['time']))} {e['currency']} {e['title']}"


# ----------------------------------------------------------------------------
# Strategy
# ----------------------------------------------------------------------------
def h4_trend(ind, i):
    j = ind["h4idx"][i]
    if j < 0 or ind["h4f"][j] is None or ind["h4s"][j] is None:
        return None
    f, s, c = ind["h4f"][j], ind["h4s"][j], ind["h4c"][j]
    if f > s and c > s:
        return "up"
    if f < s and c < s:
        return "down"
    return "flat"


def active_session(pair, hour_utc):
    """London 07-16 UTC and New York 12-21 UTC are the busy hours. Tokyo (00-07) also counts for JPY."""
    if 7 <= hour_utc < 21:
        return True
    return "JPY" in pair and 0 <= hour_utc < 7


def raw_setup(ind, i, p):
    """Basic trend + RSI pullback rule. Returns (direction, reason) or (None, None)."""
    ef, es, r, closes = ind["ef"], ind["es"], ind["rsi"], ind["close"]
    if i < 11 or None in (ef[i], es[i], es[i - 10], r[i], r[i - 1], ind["atr"][i]):
        return None, None
    price = closes[i]
    up, down = ef[i] > es[i], ef[i] < es[i]
    if p["price_filter"]:
        up, down = up and price > es[i], down and price < es[i]
    if p["trend_slope"]:
        up, down = up and es[i] > es[i - 10], down and es[i] < es[i - 10]
    if up and r[i - 1] < p["rsi_buy"] <= r[i]:
        return "BUY", f"Uptrend (EMA{EMA_FAST} above EMA{EMA_SLOW}); RSI bounced back above {p['rsi_buy']}"
    if down and r[i - 1] > p["rsi_sell"] >= r[i]:
        return "SELL", f"Downtrend (EMA{EMA_FAST} below EMA{EMA_SLOW}); RSI dropped back below {p['rsi_sell']}"
    return None, None


def score_setup(pair, ind, i, direction, sl_dist, tp_dist, news_6h=None):
    """Confidence score 0-100 with reasons for (pros) and against (cons)."""
    score, pros, cons = 50, [], []
    pip = pip_size(pair)
    c = ind["candles"][i]

    h4 = h4_trend(ind, i)
    want = "up" if direction == "BUY" else "down"
    if h4 == want:
        score += 15; pros.append("4-hour trend agrees")
    elif h4 in ("up", "down"):
        score -= 20; cons.append("4-hour trend points the other way")
    else:
        score -= 5; cons.append("4-hour trend is unclear")

    a = ind["adx"][i]
    if a is not None:
        if a >= 25:
            score += 10; pros.append(f"Strong trend (ADX {a:.0f})")
        elif a >= 20:
            score += 5; pros.append(f"Moderate trend strength (ADX {a:.0f})")
        else:
            score -= 10; cons.append(f"Weak / choppy trend (ADX {a:.0f})")

    lo = max(0, i - SWING_BARS)
    entry = ind["close"][i]
    if i > lo:
        if direction == "BUY":
            level = max(ind["high"][lo:i]); room = level - entry; name = "recent high"
        else:
            level = min(ind["low"][lo:i]); room = entry - level; name = "recent low"
        if room <= 0:
            score += 5; pros.append(f"Price is beyond the {name} (no nearby barrier)")
        elif room >= tp_dist:
            score += 10; pros.append(f"Clear room to TP before the {name}")
        elif room < sl_dist:
            score -= 15; cons.append(f"The {name} is very close ({room / pip:.0f} pips away)")
        else:
            score -= 5; cons.append(f"The {name} sits before TP ({room / pip:.0f} pips away)")

    if (direction == "BUY" and c["close"] > c["open"]) or (direction == "SELL" and c["close"] < c["open"]):
        score += 5; pros.append("Signal candle closed in the trade's direction")
    else:
        score -= 5; cons.append("Signal candle closed against the trade")

    hour = (c["time"] + timedelta(hours=INTERVAL_HOURS)).hour
    if active_session(pair, hour):
        score += 5; pros.append("Busy market hours (good liquidity)")
    else:
        score -= 10; cons.append("Quiet market hours (low liquidity)")

    avg = ind["atr_avg"][i]
    if avg:
        ratio = ind["atr"][i] / avg
        if ratio < 0.6:
            score -= 10; cons.append("Market is unusually quiet")
        elif ratio > 2.0:
            score -= 10; cons.append("Market is unusually wild")

    if news_6h:
        score -= 10; cons.append(f"High-impact news within 6h ({news_6h[0]['currency']} {news_6h[0]['title']})")

    return max(0, min(100, score)), pros, cons, h4, a


def evaluate(pair, ind, i, profile, use_filters=True, news_6h=None):
    """Return (signal or None, note). The signal is entered at the close of candle i."""
    p = PROFILES[profile]
    if p.get("only_pairs") and pair not in p["only_pairs"]:
        return None, "pair not traded at this level"
    if p.get("hours_utc"):
        h = (ind["candles"][i]["time"] + timedelta(hours=INTERVAL_HOURS)).hour
        if not (p["hours_utc"][0] <= h < p["hours_utc"][1]):
            return None, "outside this level's trading hours"
    direction, reason = raw_setup(ind, i, p)
    if not direction:
        return None, "no setup"
    price, a = ind["close"][i], ind["atr"][i]
    risk, reward = p["sl_atr"] * a, p["tp_atr"] * a
    score, pros, cons, h4, adx_v = score_setup(pair, ind, i, direction, risk, reward, news_6h)
    if use_filters:
        want = "up" if direction == "BUY" else "down"
        against = h4 in ("up", "down") and h4 != want
        if p["h4_rule"] == "agree" and h4 != want:
            return None, f"{direction} setup filtered: 4-hour trend does not agree"
        if p["h4_rule"] == "not_against" and against:
            return None, f"{direction} setup filtered: 4-hour trend is against it"
        if p["min_adx"] and (adx_v is None or adx_v < p["min_adx"]):
            return None, f"{direction} setup filtered: trend too weak (ADX {adx_v or 0:.0f})"
        if score < p["min_score"]:
            return None, f"{direction} setup filtered: confidence {score} < {p['min_score']}"
    d = decimals(pair)
    sl, tp = (price - risk, price + reward) if direction == "BUY" else (price + risk, price - reward)
    t = ind["candles"][i]["time"] + timedelta(hours=INTERVAL_HOURS)   # candle close time
    return {
        "id": f"{profile}-{pair.replace('/', '')}-{t.strftime('%Y%m%d%H%M')}",
        "profile": profile, "pair": pair, "direction": direction,
        "entry": round(price, d), "sl": round(sl, d), "tp": round(tp, d),
        "sl_pips": round(risk / pip_size(pair), 1), "tp_pips": round(reward / pip_size(pair), 1),
        "rsi": round(ind["rsi"][i], 1), "adx": None if adx_v is None else round(adx_v, 1),
        "h4_trend": h4, "score": score, "pros": pros, "cons": cons,
        "reason": reason, "created": iso(t),
        "status": "open", "closed_at": None, "exit_price": None,
    }, "setup"


def update_open_signal(sig, candles, now):
    """Check candles after entry. Returns True if the signal was closed."""
    created = from_iso(sig["created"])
    for c in candles:
        if c["time"] < created:
            continue
        half = SPREAD_PIPS.get(sig["pair"], 0) * pip_size(sig["pair"]) / 2
        if sig["direction"] == "BUY":      # a BUY is closed at the bid (mid - half spread)
            hit_sl, hit_tp = c["low"] - half <= sig["sl"], c["high"] - half >= sig["tp"]
        else:                              # a SELL is closed at the ask (mid + half spread)
            hit_sl, hit_tp = c["high"] + half >= sig["sl"], c["low"] + half <= sig["tp"]
        # If both happen inside one candle we cannot know which came first,
        # so we count it as a loss (the honest, conservative choice).
        if hit_sl:
            sig.update(status="loss", exit_price=sig["sl"], closed_at=iso(c["time"]))
            return True
        if hit_tp:
            sig.update(status="win", exit_price=sig["tp"], closed_at=iso(c["time"]))
            return True
    if now - created > timedelta(hours=EXPIRE_HOURS) and candles:
        sig.update(status="expired", exit_price=round(candles[-1]["close"], decimals(sig["pair"])),
                   closed_at=iso(now))
        return True
    return False


def snapshot(pair, ind, candles):
    """Plain-numbers summary of the chart, for the website and the Claude check-ins."""
    i = len(ind["close"]) - 1
    if i < EMA_SLOW:
        return None
    d, pip = decimals(pair), pip_size(pair)
    ef, es = ind["ef"][i], ind["es"][i]
    lo = max(0, i - SWING_BARS + 1)
    return {
        "pair": pair,
        "price": round(candles[-1]["close"], d),
        "trend_1h": "up" if ef > es else "down",
        "trend_4h": h4_trend(ind, i),
        "rsi": round(ind["rsi"][i], 1),
        "adx": None if ind["adx"][i] is None else round(ind["adx"][i], 1),
        "atr_pips": round(ind["atr"][i] / pip, 1),
        "high_48h": round(max(ind["high"][lo:]), d),
        "low_48h": round(min(ind["low"][lo:]), d),
    }


# ----------------------------------------------------------------------------
# Alerts
# ----------------------------------------------------------------------------
def send_telegram(text):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    for attempt in range(1, 4):          # retry: short network hiccups happen
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              data={"chat_id": chat, "text": text, "parse_mode": "HTML",
                                    "disable_web_page_preview": "true"}, timeout=20)
            if r.ok:
                return
            print(f"Telegram error (attempt {attempt}):", r.text)
            if r.status_code < 500 and r.status_code != 429:
                return                   # a real error (e.g. bad token) - retrying won't help
        except Exception as e:
            print(f"Telegram error (attempt {attempt}):", e)
        time.sleep(5 * attempt)


def send_email(subject, text):
    user, pw = os.getenv("GMAIL_ADDRESS"), os.getenv("GMAIL_APP_PASSWORD")
    if not user or not pw:
        return
    to = os.getenv("EMAIL_TO") or user
    msg = MIMEText(text.replace("<b>", "").replace("</b>", ""), "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    for attempt in range(1, 4):          # retry: short network hiccups happen
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
                s.login(user, pw.replace(" ", ""))
                s.sendmail(user, [x.strip() for x in to.split(",")], msg.as_string())
            return
        except smtplib.SMTPAuthenticationError as e:
            print("Email error (login refused - check GMAIL_APP_PASSWORD):", e)
            return
        except Exception as e:
            print(f"Email error (attempt {attempt}):", e)
            time.sleep(5 * attempt)


def notify(subject, text):
    site = os.getenv("SITE_URL")
    if site:
        text += f"\n\nDashboard: {site}"
    text += "\n\nRule-based suggestion, not financial advice."
    send_telegram(text)
    send_email(subject, text)


def tag(profile):
    p = PROFILES.get(profile, PROFILES["moderate"])
    return f"{p['icon']} {p['label'].upper()}"


def new_signal_message(s, upcoming=None):
    arrow = "🟢 BUY" if s["direction"] == "BUY" else "🔴 SELL"
    msg = (f"[{tag(s['profile'])}]\n"
           + (f"{ALERT_WARNINGS[s['profile']]}\n" if s["profile"] in ALERT_WARNINGS else "")
           + f"<b>{arrow} {s['pair']}</b>\n"
           f"Entry: {s['entry']}\n"
           f"Take Profit: {s['tp']}  (+{s['tp_pips']} pips)\n"
           f"Stop Loss: {s['sl']}  (-{s['sl_pips']} pips)\n"
           f"Confidence: <b>{s['score']}/100</b>\n"
           f"Why: {s['reason']}\n")
    if s.get("pros"):
        msg += "\n".join(f"✅ {x}" for x in s["pros"]) + "\n"
    if s.get("cons"):
        msg += "\n".join(f"⚠️ {x}" for x in s["cons"]) + "\n"
    msg += f"Time: {my_time(from_iso(s['created']))}"
    if upcoming is None:
        msg += "\n\n📰 News check unavailable (Forex Factory feed could not be read)."
    elif upcoming:
        msg += f"\n\n📰 High-impact news in next {NEWS_ALERT_HOURS}h (Forex Factory):\n"
        msg += "\n".join(news_line(e) for e in upcoming[:5])
    else:
        msg += f"\n\n📰 No high-impact news for this pair in the next {NEWS_ALERT_HOURS}h."
    return msg


def news_warning_message(sigs, e):
    lines = "\n".join(f"• [{tag(s['profile'])}] {s['direction']} {s['pair']} (entry {s['entry']})" for s in sigs)
    return (f"<b>⚠️ High-impact news soon</b>\n"
            f"{news_line(e)}\n"
            f"Forecast: {e['forecast'] or '-'} | Previous: {e['previous'] or '-'}\n\n"
            f"Open trades affected:\n{lines}\n\n"
            f"High-impact news can make price jump fast, past your SL or TP.")


def result_message(s, stats):
    icon = {"win": "✅ TP HIT", "loss": "❌ SL HIT", "expired": "⏱ EXPIRED"}[s["status"]]
    return (f"[{tag(s['profile'])}]\n"
            f"<b>{icon}: {s['direction']} {s['pair']}</b>\n"
            f"Entry {s['entry']} → Exit {s['exit_price']}\n"
            f"{PROFILES[s['profile']]['label']} accuracy: {stats}")


def accuracy_text(signals):
    w = sum(1 for s in signals if s["status"] == "win")
    l = sum(1 for s in signals if s["status"] == "loss")
    if w + l == 0:
        return "no closed trades yet"
    return f"{100 * w / (w + l):.1f}% ({w} wins / {l} losses)"


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def run(fetch=fetch_candles, now=None, news_fetch=fetch_news):
    now = now or datetime.now(timezone.utc)
    api_key = os.getenv("TWELVEDATA_API_KEY", "")
    data = load_data()
    info = strategy_info()
    changed = data.get("strategy") != info
    data["strategy"] = info
    signals = data["signals"]
    warned = data.setdefault("news_warned", [])
    outbox = []          # (subject, text, profile or None)

    # Market snapshot about once an hour (keeps the number of saves low)
    last_snap = data.get("snapshot_time")
    take_snapshot = (not last_snap) or (now - from_iso(last_snap) >= timedelta(minutes=SNAPSHOT_EVERY_MIN))
    snaps = []

    # Economic calendar (Forex Factory). If it fails, the bot still runs.
    try:
        events = news_fetch()
    except Exception as e:
        print("News feed error:", e)
        events = None
    if events is not None:
        upcoming = [e for e in events if e["impact"] in ("High", "Medium")
                    and from_iso(e["time"]) >= now - timedelta(hours=2)]
        if upcoming != data.get("news"):
            data["news"] = upcoming
            changed = True

    for n, pair in enumerate(PAIRS):
        if n:
            time.sleep(2)   # stay inside the free plan's per-minute limit
        try:
            candles = fetch(pair, api_key)
        except Exception as e:
            print(f"[{pair}] could not get prices: {e}")
            continue
        closed = [c for c in candles if c["time"] + timedelta(hours=INTERVAL_HOURS) <= now]
        ind = indicators(closed) if len(closed) > EMA_SLOW + 15 else None
        if ind and take_snapshot:
            snap = snapshot(pair, ind, candles)
            if snap:
                snaps.append(snap)

        # 1) Update open suggestions (all risk levels)
        for s in signals:
            if s["pair"] == pair and s["status"] == "open" and update_open_signal(s, candles, now):
                changed = True
                same = [x for x in signals if x["profile"] == s["profile"]]
                outbox.append((f"{s['pair']} {s['status'].upper()}",
                               result_message(s, accuracy_text(same)), s["profile"]))

        # 2) One warning per news event, listing every open trade it affects
        open_here = [s for s in signals if s["pair"] == pair and s["status"] == "open"]
        if open_here:
            for e in news_for(pair, events, now, 0, NEWS_WARN_MIN):
                key = f"{pair}|{e['time']}|{e['title']}"
                if key not in warned:
                    warned.append(key)
                    changed = True
                    outbox.append((f"News warning: {pair}", news_warning_message(open_here, e), None))

        # 3) New setups, separately for each risk level (max one open per pair per level)
        news_6h = news_for(pair, events, now, 0, 360) if events else None
        for prof, p in PROFILES.items():
            label = f"[{pair}][{p['label']}]"
            if any(s["pair"] == pair and s["profile"] == prof and s["status"] == "open" for s in signals):
                print(f"{label} open suggestion, waiting for TP/SL")
                continue
            if not ind:
                print(f"{label} not enough data")
                continue
            sig, note = evaluate(pair, ind, len(closed) - 1, prof, True, news_6h)
            if not sig or any(s["id"] == sig["id"] for s in signals):
                print(f"{label} {note if not sig else 'no new setup'}")
                continue
            impacts = ("High", "Medium") if p["news_medium"] else ("High",)
            block = news_for(pair, events, now, -p["news_after"], p["news_before"], impacts)
            if block:
                print(f"{label} setup SKIPPED because of news: {block[0]['currency']} {block[0]['title']}")
                continue
            upcoming = None if events is None else news_for(pair, events, now, 0, NEWS_ALERT_HOURS * 60)
            sig["news_note"] = ("feed unavailable" if upcoming is None else
                                "; ".join(f"{e['currency']} {e['title']}" for e in upcoming[:5]) or "none in next 24h")
            signals.append(sig)
            changed = True
            outbox.append((f"Forex signal [{p['label']}]: {sig['direction']} {pair}",
                           new_signal_message(sig, upcoming), prof))
            print(f"{label} NEW {sig['direction']} at {sig['entry']} (confidence {sig['score']})")

    del warned[:-200]   # keep the list short

    if take_snapshot and snaps:
        data["snapshot"] = snaps
        data["snapshot_time"] = iso(now)
        changed = True

    for subject, text, prof in outbox:
        if prof is None or prof in ALERT_PROFILES:
            notify(subject, text)

    if changed or not os.path.exists(DATA_FILE):
        data["updated"] = iso(now)
        save_data(data)
    return data


def resend_last_result():
    """Re-send the most recent TP/SL/expired result to Telegram only (if a message was lost)."""
    data = load_data()
    done = [s for s in data["signals"] if s["status"] != "open" and s.get("closed_at")]
    if not done:
        print("No closed trades to re-send.")
        return
    s = max(done, key=lambda x: x["closed_at"])
    same = [x for x in data["signals"] if x["profile"] == s["profile"]]
    text = "(re-sent)\n" + result_message(s, accuracy_text(same))
    site = os.getenv("SITE_URL")
    if site:
        text += f"\n\nDashboard: {site}"
    send_telegram(text)
    print("Re-sent to Telegram:", s["id"], s["status"])


if __name__ == "__main__":
    if "--resend-last" in sys.argv:
        resend_last_result()
    elif "--test-alert" in sys.argv:
        notify("Forex bot test", "<b>✅ Test alert</b>\nYour forex signal bot can reach you.")
        print("Test alert sent (check Telegram / Gmail).")
    else:
        if not os.getenv("TWELVEDATA_API_KEY"):
            sys.exit("Missing TWELVEDATA_API_KEY secret. See README step 3.")
        run()
