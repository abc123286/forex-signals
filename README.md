# Forex Signal Bot (free, runs 24/7 on GitHub)

What it does:
- **Live chart website**: a TradingView chart for EUR/USD, GBP/USD, USD/JPY and XAU/USD (gold)
- **Suggestions**: every 15 minutes it checks the 1-hour chart. When the strategy finds a setup it sends you **Entry, Take Profit (TP) and Stop Loss (SL)** on Telegram and/or Gmail
- **History + accuracy**: every suggestion is saved. When price hits TP (win) or SL (loss), you get a result alert, and the website updates the accuracy %

> ⚠️ Educational, rule-based tool, **not financial advice**. Test it on a **demo account** for a few weeks before risking real money.

---

## How the strategy works

Think of it like **surfing**: first find which way the wave is going (trend), then wait for a small dip so you can get on at a better price (pullback).

- **Trend:** 50-candle average above the 200-candle average = uptrend (look for BUY); below = downtrend (look for SELL).
- **Pullback ends:** RSI dips and then climbs back (BUY), or rises and then drops back (SELL).
- **SL / TP** are set from **ATR** (how much price normally moves in one hour), so they adjust to calm or wild markets.

### Three risk levels (each has its own tab, history and accuracy)

| | 🛡️ Low risk | ⚖️ Moderate | 🔥 High risk |
|---|---|---|---|
| Pullback needed | Deep (RSI 35 / 65) + trend must be rising/falling | Normal (RSI 40 / 60) | Small (RSI 45 / 55), no extra filters |
| Stop Loss | 2 × ATR (wide) | 1.5 × ATR | 1 × ATR (tight) |
| Take Profit | 2 × ATR (1 : 1) | 2.25 × ATR (1.5 : 1) | 2 × ATR (2 : 1) |
| Break-even win rate | ~50% | ~40% | ~33% |
| News pause (Forex Factory) | 2h before → 1h after **high & medium** news | 1h before → 30 min after high news | 15 min before → 15 min after high news |
| Expect | Fewest trades, higher win %, smaller wins | Balanced | Most trades, lower win %, bigger wins |

- Only **one open suggestion per pair per risk level** at a time.
- A suggestion with no TP/SL after **5 days** is marked "expired" (not counted in accuracy).
- Every new alert lists the **high-impact news in the next 24 hours**, and you get a ⚠️ warning when high-impact news is less than 60 minutes away and you have an open trade.
- If the Forex Factory feed can't be read, the bot still works and says so in the alert.
- To stop Telegram alerts for a level, edit `ALERT_PROFILES` at the top of `signals.py` (the website still records all levels).

"Low risk" means lower risk **compared with the other levels**, not risk-free.

### Smart filters & confidence score
Every setup gets a **confidence score (0–100)** with ✅ reasons for and ⚠️ reasons against:
- **4-hour trend** agrees (+15) / points the other way (−20)
- **Trend strength (ADX)**: strong ≥25 (+10), moderate 20–25 (+5), weak <20 (−10)
- **Room to TP** before the recent 48h high/low (+10), or a barrier very close (−15)
- **Signal candle** closed in the trade's direction (+5 / −5)
- **Busy market hours** London/New York (+5) vs quiet hours (−10)
- **Unusual volatility** (too quiet or too wild) (−10)
- **High-impact news within 6h** (−10)

Minimum confidence: Low 70 (and 4H must agree, ADX ≥ 20) · Moderate 55 (4H not against) · High 40.

### Backtest
`backtest.py` replays every risk level on ~8 months of past 1-hour prices, **with** and **without** the smart filters,
and checks whether higher confidence really did better. Results appear on each risk tab of the website.
It runs by itself every Saturday, or anytime: **Actions → Backtest → Run workflow**.
Spreads/fees and the news filter are not included, so real results will be a bit worse.

### Market snapshot
About once an hour the bot saves a snapshot (price, 1H/4H trend, RSI, ADX, normal move, 48h high/low) for the website and the Claude check-ins.

---

## Setup (about 30 minutes, no coding needed)

### Step 1: Create the GitHub project
1. Sign up for free at **github.com**.
2. Click **+** (top right) → **New repository**.
3. Name: `forex-signals`. Choose **Public** (this keeps it free with unlimited run time). Click **Create repository**.
4. Click **uploading an existing file**. Unzip the project on your computer and drag in **`signals.py`, `README.md` and the `docs` folder**. Click **Commit changes**.

### Step 2: Add the schedule file (`.github` folder)
Folders that start with a dot are often hidden and don't upload by drag-and-drop, so create this one by hand:
1. In your repository click **Add file → Create new file**.
2. For the file name type exactly: `.github/workflows/signals.yml`
3. Open `signals.yml` from the zip with Notepad (Windows) or TextEdit (Mac), copy everything and paste it in.
4. Click **Commit changes**.

### Step 3: Get a free price-data key (Twelve Data)
1. Sign up for free at **twelvedata.com**.
2. Open your dashboard and copy your **API key**.

### Step 4: Create your Telegram alert bot
1. In Telegram, search for **@BotFather** → send `/newbot` → follow the steps. Copy the **token** it gives you (looks like `123456:ABC-xyz...`).
2. Open your new bot and send it any message (e.g. "hi").
3. In your browser, open: `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` (replace `<YOUR_TOKEN>`).
4. Find `"chat":{"id":` and copy the number after it. That is your **chat ID**.

### Step 5 (optional): Gmail alerts
1. Your Google account needs **2-Step Verification** turned on.
2. Go to **myaccount.google.com/apppasswords**, create an app password called "forex bot", and copy the 16 letters.

### Step 6: Save your keys as secrets
In your repository: **Settings → Secrets and variables → Actions → New repository secret**. Add each one:

| Name | Value |
|---|---|
| `TWELVEDATA_API_KEY` | your Twelve Data key (required) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | your chat ID number |
| `GMAIL_ADDRESS` | your Gmail address (optional) |
| `GMAIL_APP_PASSWORD` | the 16-letter app password (optional) |
| `EMAIL_TO` | where to send emails, if different (optional; separate several with commas) |

Secrets are private. Even though the project is public, nobody can see them.

### Step 7: Turn on the website
**Settings → Pages** → Source: **Deploy from a branch** → Branch: **main**, folder **/docs** → **Save**.
After 1–2 minutes your dashboard is at: `https://YOUR-USERNAME.github.io/forex-signals/`

### Step 8: Start the bot
1. Open the **Actions** tab. If asked, click **I understand my workflows, enable them**.
2. Click **Forex signals** → **Run workflow** → tick **Only send a test alert** → **Run workflow**. You should get a test message on Telegram/Gmail within a minute.
3. Run it once more **without** the tick. Click the run to see the log, for example `[EUR/USD] no setup`.
4. Done! From now on it runs by itself every 15 minutes (Sunday–Friday, UTC).

---

## Good to know
- **Not every 15 minutes exactly.** GitHub sometimes starts scheduled runs a few minutes late. That's fine for a 1-hour strategy.
- **Signals are not frequent.** Expect a few per week across all pairs. "No setup" is normal.
- **Gold (XAU/USD)**: if the log shows an error for XAU/USD, your free Twelve Data plan may not include it. Remove it from `PAIRS` in `signals.py`, or the bot will keep skipping it.
- **Entry price** is the close of the 1-hour candle. Your broker's price will be slightly different (spread), so the alert arrives a few minutes after the candle closes.
- **If the bot stops**: GitHub pauses scheduled jobs in public projects after 60 days with no changes. If that happens, open **Actions → Forex signals → Enable workflow**.
- **Reset history**: replace `docs/data/signals.json` with `{"strategy": {}, "signals": []}`.
