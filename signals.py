#!/usr/bin/env python3
"""
Forex signal bot with three risk levels (Low / Moderate / High).

Runs on GitHub Actions every 15 minutes:
  1. Downloads recent 1-hour candles for each pair (Twelve Data, free plan).
  2. Reads the Forex Factory economic calendar (high/medium impact news).
  3. For each risk level: checks open suggestions (TP hit = win, SL hit = loss),
     warns about big news for open trades, and looks for new setups.
  4. Sends alerts (Telegram and/or Gmail).
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
INTERVAL_HOURS = 1          # 1-hour candles (shared by all risk levels)
CANDLES = 300               # how many candles to download
EMA_FAST = 50
EMA_SLOW = 200
RSI_LEN = 14
ATR_LEN = 14
EXPIRE_HOURS = 120          # close a suggestion as "expired" after 5 days with no TP/SL
NEWS_WARN_MIN = 60          # warn about open trades when high-impact news is within 60 min
NEWS_ALERT_HOURS = 24       # list high-impact news for the next 24h inside each new alert
MY_TIMEZONE = timezone(timedelta(hours=8))   # Malaysia time for alert messages

# Which risk levels send Telegram/Gmail alerts. All levels are always recorded on the website.
ALERT_PROFILES = ["low", "moderate", "high"]

PROFILES = {
    "low": {
        "label": "Low risk", "icon": "🛡️",
        "rsi_buy": 35, "rsi_sell": 65,      # needs a deeper pullback
        "sl_atr": 2.0, "tp_atr": 2.0,       # wide SL, TP same size as SL (1 : 1)
        "trend_slope": True,                # long-term trend must also be rising/falling
        "price_filter": True,               # price must be on the right side of EMA200
        "news_before": 120, "news_after": 60, "news_medium": True,
        "desc": "Very picky: only strong, clear trends after a deep pullback. Wide stop loss and "
                "TP equal to SL, so wins should be more frequent but smaller. Pauses 2h before to "
                "1h after high AND medium impact news.",
    },
    "moderate": {
        "label": "Moderate", "icon": "⚖️",
        "rsi_buy": 40, "rsi_sell": 60,
        "sl_atr": 1.5, "tp_atr": 2.25,      # reward : risk = 1.5 : 1
        "trend_slope": False,
        "price_filter": True,
        "news_before": 60, "news_after": 30, "news_medium": False,
        "desc": "Balanced: trend + normal pullback. TP is 1.5x the SL. Pauses 1h before to 30 min "
                "after high impact news.",
    },
    "high": {
        "label": "High risk", "icon": "🔥",
        "rsi_buy": 45, "rsi_sell": 55,      # small pullbacks count -> more trades
        "sl_atr": 1.0, "tp_atr": 2.0,       # tight SL, TP = 2x SL (2 : 1)
        "trend_slope": False,
        "price_filter": False,
        "news_before": 15, "news_after": 15, "news_medium": False,
        "desc": "Least picky: more trades, tight stop loss, TP is 2x the SL. Expect more losing "
                "trades but bigger wins. Only pauses 15 min around high impact news.",
    },
}

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(ROOT, "docs", "data", "signals.json")
API_URL = "https://api.twelvedata.com/time_series"
NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"   # Forex Factory calendar feed


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
    return dt.astimezone(MY_TIMEZONE).strftime("%a %d %b, %I:%M %p (MYT)")


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"signals": []}
    for s in data.get("signals", []):
        s.setdefault("profile", "moderate")      # older suggestions = moderate
    data.setdefault("signals", [])
    return data


def save_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def strategy_info():
    return {
        "name": "Trend + RSI pullback",
        "timeframe": f"{INTERVAL_HOURS}H",
        "pairs": PAIRS,
        "profiles": {
            k: {"label": p["label"], "icon": p["icon"], "desc": p["desc"],
                "reward_risk": round(p["tp_atr"] / p["sl_atr"], 2),
                "sl_atr": p["sl_atr"], "tp_atr": p["tp_atr"],
                "rsi_buy": p["rsi_buy"], "rsi_sell": p["rsi_sell"]}
            for k, p in PROFILES.items()
        },
    }


# ----------------------------------------------------------------------------
# Price data
# ----------------------------------------------------------------------------
def fetch_candles(pair, api_key):
    params = {"symbol": pair, "interval": f"{INTERVAL_HOURS}h", "outputsize": CANDLES,
              "timezone": "UTC", "apikey": api_key}
    r = requests.get(API_URL, params=params, timeout=30)
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
    if base == "XAU":            # gold mostly reacts to US news
        return {"USD"}
    return {base, quote}


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
def indicators(closed):
    closes = [c["close"] for c in closed]
    return {
        "close": closes,
        "ef": ema(closes, EMA_FAST),
        "es": ema(closes, EMA_SLOW),
        "rsi": rsi(closes, RSI_LEN),
        "atr": atr([c["high"] for c in closed], [c["low"] for c in closed], closes, ATR_LEN),
    }


def find_setup(pair, closed, profile="moderate", ind=None):
    """Look at the latest *closed* candle. Return a new signal dict or None."""
    p = PROFILES[profile]
    if len(closed) < EMA_SLOW + 15:
        return None
    ind = ind or indicators(closed)
    ef, es, r, a, closes = ind["ef"], ind["es"], ind["rsi"], ind["atr"], ind["close"]
    i = len(closed) - 1
    if None in (ef[i], es[i], es[i - 10], r[i], r[i - 1], a[i]):
        return None

    price = closes[i]
    up = ef[i] > es[i]
    down = ef[i] < es[i]
    if p["price_filter"]:
        up = up and price > es[i]
        down = down and price < es[i]
    if p["trend_slope"]:
        up = up and es[i] > es[i - 10]
        down = down and es[i] < es[i - 10]

    direction = None
    if up and r[i - 1] < p["rsi_buy"] <= r[i]:
        direction = "BUY"
        reason = f"Uptrend (EMA{EMA_FAST} above EMA{EMA_SLOW}); RSI bounced back above {p['rsi_buy']}"
    elif down and r[i - 1] > p["rsi_sell"] >= r[i]:
        direction = "SELL"
        reason = f"Downtrend (EMA{EMA_FAST} below EMA{EMA_SLOW}); RSI dropped back below {p['rsi_sell']}"
    if not direction:
        return None

    d = decimals(pair)
    risk, reward = p["sl_atr"] * a[i], p["tp_atr"] * a[i]
    sl, tp = (price - risk, price + reward) if direction == "BUY" else (price + risk, price - reward)
    signal_time = closed[i]["time"] + timedelta(hours=INTERVAL_HOURS)   # candle close time
    return {
        "id": f"{profile}-{pair.replace('/', '')}-{signal_time.strftime('%Y%m%d%H%M')}",
        "profile": profile,
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


def tag(profile):
    p = PROFILES.get(profile, PROFILES["moderate"])
    return f"{p['icon']} {p['label'].upper()}"


def new_signal_message(s, upcoming=None):
    arrow = "🟢 BUY" if s["direction"] == "BUY" else "🔴 SELL"
    msg = (f"[{tag(s['profile'])}]\n"
           f"<b>{arrow} {s['pair']}</b>\n"
           f"Entry: {s['entry']}\n"
           f"Take Profit: {s['tp']}  (+{s['tp_pips']} pips)\n"
           f"Stop Loss: {s['sl']}  (-{s['sl_pips']} pips)\n"
           f"Why: {s['reason']}\n"
           f"Time: {my_time(from_iso(s['created']))}")
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
        ind = indicators(closed) if len(closed) >= EMA_SLOW + 15 else None

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
        for prof, p in PROFILES.items():
            label = f"[{pair}][{p['label']}]"
            if any(s["pair"] == pair and s["profile"] == prof and s["status"] == "open" for s in signals):
                print(f"{label} open suggestion, waiting for TP/SL")
                continue
            sig = find_setup(pair, closed, prof, ind) if ind else None
            if not sig or any(s["id"] == sig["id"] for s in signals):
                print(f"{label} no setup")
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
            print(f"{label} NEW {sig['direction']} at {sig['entry']}")

    del warned[:-200]   # keep the list short

    for subject, text, prof in outbox:
        if prof is None or prof in ALERT_PROFILES:
            notify(subject, text)

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
