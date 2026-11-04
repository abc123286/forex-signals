#!/usr/bin/env python3
"""
Forex signal bot.

Runs on GitHub Actions every 15 minutes:
  1. Downloads recent 1-hour candles for each pair (Twelve Data, free plan).
  2. Checks open suggestions: did price hit Take Profit (win) or Stop Loss (loss)?
  3. Looks for a new setup using the "Trend + RSI pullback" strategy.
  4. Sends alerts (Telegram and/or Gmail) for new suggestions and results.
  5. Saves everything to docs/data/signals.json (shown on the website).

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
INTERVAL_HOURS = 1          # 1-hour candles
CANDLES = 300               # how many candles to download
EMA_FAST = 50               # trend filter: fast moving average
EMA_SLOW = 200              # trend filter: slow moving average
RSI_LEN = 14
ATR_LEN = 14
RSI_BUY_LEVEL = 40          # uptrend: RSI dips under 40, then climbs back above -> BUY
RSI_SELL_LEVEL = 60         # downtrend: RSI rises over 60, then drops back below -> SELL
SL_ATR = 1.5                # Stop Loss  = 1.5 x ATR away from entry
TP_ATR = 2.25               # Take Profit = 2.25 x ATR away (reward:risk = 1.5 : 1)
EXPIRE_HOURS = 120          # close a suggestion as "expired" after 5 days with no TP/SL
MY_TIMEZONE = timezone(timedelta(hours=8))   # Malaysia time for alert messages

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(ROOT, "docs", "data", "signals.json")
API_URL = "https://api.twelvedata.com/time_series"


# ----------------------------------------------------------------------------
# Indicators
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


def rsi(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains, losses = 0.0, 0.0
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


def atr(highs, lows, closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    prev = sum(trs[1:n + 1]) / n
    out[n] = prev
    for i in range(n + 1, len(closes)):
        prev = (prev * (n - 1) + trs[i]) / n
        out[i] = prev
    return out


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def decimals(pair):
    if "JPY" in pair:
        return 3
    if "XAU" in pair:
        return 2
    return 5


def pip_size(pair):
    if "JPY" in pair:
        return 0.01
    if "XAU" in pair:
        return 0.1
    return 0.0001


def parse_time(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def my_time(dt):
    return dt.astimezone(MY_TIMEZONE).strftime("%d %b %Y, %I:%M %p (MYT)")


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"strategy": {}, "signals": []}


def save_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def strategy_info():
    return {
        "name": "Trend + RSI pullback",
        "timeframe": f"{INTERVAL_HOURS}H",
        "rules": (f"BUY when EMA{EMA_FAST} > EMA{EMA_SLOW} (uptrend) and RSI{RSI_LEN} crosses back above "
                  f"{RSI_BUY_LEVEL}. SELL when EMA{EMA_FAST} < EMA{EMA_SLOW} (downtrend) and RSI{RSI_LEN} "
                  f"crosses back below {RSI_SELL_LEVEL}. SL = {SL_ATR} x ATR{ATR_LEN}, TP = {TP_ATR} x ATR{ATR_LEN}."),
        "reward_risk": round(TP_ATR / SL_ATR, 2),
        "pairs": PAIRS,
    }


# ----------------------------------------------------------------------------
# Price data
# ----------------------------------------------------------------------------
def fetch_candles(pair, api_key):
    params = {
        "symbol": pair,
        "interval": f"{INTERVAL_HOURS}h",
        "outputsize": CANDLES,
        "timezone": "UTC",
        "apikey": api_key,
    }
    r = requests.get(API_URL, params=params, timeout=30)
    r.raise_for_status()
    js = r.json()
    if js.get("status") != "ok" or "values" not in js:
        raise RuntimeError(js.get("message", "unknown error from Twelve Data"))
    candles = []
    for v in js["values"]:
        candles.append({
            "time": parse_time(v["datetime"]),
            "open": float(v["open"]),
            "high": float(v["high"]),
            "low": float(v["low"]),
            "close": float(v["close"]),
        })
    candles.sort(key=lambda c: c["time"])
    return candles


# ----------------------------------------------------------------------------
# Strategy
# ----------------------------------------------------------------------------
def find_setup(pair, closed):
    """Look at the latest *closed* candle. Return a new signal dict or None."""
    if len(closed) < EMA_SLOW + 5:
        return None
    closes = [c["close"] for c in closed]
    highs = [c["high"] for c in closed]
    lows = [c["low"] for c in closed]
    ef, es = ema(closes, EMA_FAST), ema(closes, EMA_SLOW)
    r, a = rsi(closes, RSI_LEN), atr(highs, lows, closes, ATR_LEN)
    i = len(closed) - 1
    if None in (ef[i], es[i], r[i], r[i - 1], a[i]):
        return None

    price = closes[i]
    direction = None
    if ef[i] > es[i] and price > es[i] and r[i - 1] < RSI_BUY_LEVEL <= r[i]:
        direction = "BUY"
        reason = f"Uptrend (EMA{EMA_FAST} above EMA{EMA_SLOW}); RSI bounced back above {RSI_BUY_LEVEL}"
    elif ef[i] < es[i] and price < es[i] and r[i - 1] > RSI_SELL_LEVEL >= r[i]:
        direction = "SELL"
        reason = f"Downtrend (EMA{EMA_FAST} below EMA{EMA_SLOW}); RSI dropped back below {RSI_SELL_LEVEL}"
    if not direction:
        return None

    d = decimals(pair)
    risk = SL_ATR * a[i]
    reward = TP_ATR * a[i]
    if direction == "BUY":
        sl, tp = price - risk, price + reward
    else:
        sl, tp = price + risk, price - reward
    signal_time = closed[i]["time"] + timedelta(hours=INTERVAL_HOURS)   # candle close time
    return {
        "id": f"{pair.replace('/', '')}-{signal_time.strftime('%Y%m%d%H%M')}",
        "pair": pair,
        "direction": direction,
        "entry": round(price, d),
        "sl": round(sl, d),
        "tp": round(tp, d),
        "sl_pips": round(risk / pip_size(pair), 1),
        "tp_pips": round(reward / pip_size(pair), 1),
        "rsi": round(r[i], 1),
        "reason": reason,
        "created": iso(signal_time),
        "status": "open",
        "closed_at": None,
        "exit_price": None,
    }


def update_open_signal(sig, candles, now):
    """Check candles after entry. Returns True if the signal was closed."""
    created = from_iso(sig["created"])
    d = decimals(sig["pair"])
    for c in candles:
        if c["time"] < created:
            continue
        if sig["direction"] == "BUY":
            hit_sl, hit_tp = c["low"] <= sig["sl"], c["high"] >= sig["tp"]
        else:
            hit_sl, hit_tp = c["high"] >= sig["sl"], c["low"] <= sig["tp"]
        # If both happen inside one candle we cannot know which came first,
        # so we count it as a loss (the honest, conservative choice).
        if hit_sl:
            sig.update(status="loss", exit_price=sig["sl"], closed_at=iso(c["time"]))
            return True
        if hit_tp:
            sig.update(status="win", exit_price=sig["tp"], closed_at=iso(c["time"]))
            return True
    if now - created > timedelta(hours=EXPIRE_HOURS) and candles:
        sig.update(status="expired", exit_price=round(candles[-1]["close"], d), closed_at=iso(now))
        return True
    return False


# ----------------------------------------------------------------------------
# Alerts
# ----------------------------------------------------------------------------
def send_telegram(text):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": "true"}, timeout=20)
        if not r.ok:
            print("Telegram error:", r.text)
    except Exception as e:
        print("Telegram error:", e)


def send_email(subject, text):
    user, pw = os.getenv("GMAIL_ADDRESS"), os.getenv("GMAIL_APP_PASSWORD")
    if not user or not pw:
        return
    to = os.getenv("EMAIL_TO") or user
    msg = MIMEText(text.replace("<b>", "").replace("</b>", ""), "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pw.replace(" ", ""))
            s.sendmail(user, [x.strip() for x in to.split(",")], msg.as_string())
    except Exception as e:
        print("Email error:", e)


def notify(subject, text):
    site = os.getenv("SITE_URL")
    if site:
        text += f"\n\nDashboard: {site}"
    text += "\n\nRule-based suggestion, not financial advice."
    send_telegram(text)
    send_email(subject, text)


def new_signal_message(s):
    arrow = "🟢 BUY" if s["direction"] == "BUY" else "🔴 SELL"
    return (f"<b>{arrow} {s['pair']}</b>\n"
            f"Entry: {s['entry']}\n"
            f"Take Profit: {s['tp']}  (+{s['tp_pips']} pips)\n"
            f"Stop Loss: {s['sl']}  (-{s['sl_pips']} pips)\n"
            f"Why: {s['reason']}\n"
            f"Time: {my_time(from_iso(s['created']))}")


def result_message(s, stats):
    icon = {"win": "✅ TP HIT", "loss": "❌ SL HIT", "expired": "⏱ EXPIRED"}[s["status"]]
    return (f"<b>{icon}: {s['direction']} {s['pair']}</b>\n"
            f"Entry {s['entry']} → Exit {s['exit_price']}\n"
            f"Overall accuracy: {stats}")


def accuracy_text(signals):
    w = sum(1 for s in signals if s["status"] == "win")
    l = sum(1 for s in signals if s["status"] == "loss")
    if w + l == 0:
        return "no closed trades yet"
    return f"{100 * w / (w + l):.1f}% ({w} wins / {l} losses)"


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def run(fetch=fetch_candles, now=None):
    now = now or datetime.now(timezone.utc)
    api_key = os.getenv("TWELVEDATA_API_KEY", "")
    data = load_data()
    data["strategy"] = strategy_info()
    signals = data["signals"]
    changed = False
    messages = []

    for n, pair in enumerate(PAIRS):
        if n:
            time.sleep(2)   # stay inside the free plan's per-minute limit
        try:
            candles = fetch(pair, api_key)
        except Exception as e:
            print(f"[{pair}] could not get prices: {e}")
            continue
        # Only candles that have fully closed are used for new signals.
        closed = [c for c in candles if c["time"] + timedelta(hours=INTERVAL_HOURS) <= now]

        # 1) Update open suggestions
        for s in signals:
            if s["pair"] == pair and s["status"] == "open":
                if update_open_signal(s, candles, now):
                    changed = True
                    messages.append((f"{s['pair']} {s['status'].upper()}", s))

        # 2) Look for a new one (max one open suggestion per pair)
        if any(s["pair"] == pair and s["status"] == "open" for s in signals):
            print(f"[{pair}] has an open suggestion, waiting for TP/SL")
            continue
        sig = find_setup(pair, closed)
        if sig and not any(s["id"] == sig["id"] for s in signals):
            signals.append(sig)
            changed = True
            messages.append(("NEW", sig))
            print(f"[{pair}] NEW {sig['direction']} at {sig['entry']}")
        else:
            print(f"[{pair}] no setup")

    for kind, s in messages:
        if kind == "NEW":
            notify(f"Forex signal: {s['direction']} {s['pair']}", new_signal_message(s))
        else:
            notify(f"Forex result: {kind}", result_message(s, accuracy_text(signals)))

    if changed or not os.path.exists(DATA_FILE):
        data["updated"] = iso(now)
        save_data(data)
    return data


if __name__ == "__main__":
    if "--test-alert" in sys.argv:
        notify("Forex bot test", "<b>✅ Test alert</b>\nYour forex signal bot can reach you.")
        print("Test alert sent (check Telegram / Gmail).")
    else:
        if not os.getenv("TWELVEDATA_API_KEY"):
            sys.exit("Missing TWELVEDATA_API_KEY secret. See README step 3.")
        run()
