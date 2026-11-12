# ZEUS Terminal — user guide

[中文 → USER_GUIDE_zh.md](USER_GUIDE_zh.md) · [Feature checklist](FEATURES.md) · [Back to README](../README.md)

This guide takes you from a fresh download to a normal trading day. Every feature is listed in the [feature checklist](FEATURES.md).

## 1. Install and start

You need Python 3.10 or newer on macOS, Linux or Windows.

```bash
git clone https://github.com/jackson-cmd/zeus-options-terminal
cd zeus-options-terminal
./start.sh
```

- **macOS:** you can also double-click `start.command`.
- **Windows:** double-click `start.bat`.
- The first run creates a virtual environment in `.venv` and installs the requirements; this takes a minute or two. Later starts take a few seconds.
- The browser opens **http://localhost:8765** by itself. To stop the terminal, press Ctrl+C in its window.
- Port 8765 is taken? Start with `PORT=8790 ./start.sh`, or change the port in ⚙ Settings → General.
- **Install it as an app:** in Chrome or Edge, use "Install ZEUS Terminal" in the address bar. It then opens in its own window.

**Updating.** Run `git pull`, then install any new requirements once and start again:

```bash
.venv/bin/python -m pip install -r requirements.txt
```

On Windows the command is `.venv\Scripts\python -m pip install -r requirements.txt`. Deleting `.venv` also works: the next start rebuilds it.

## 2. First-time setup in ⚙ Settings

Click **⚙** in the top bar. Everything is entered here; you never edit a file by hand. Secrets are write-only: after you save one, the page only shows *set* or *not set*.

### Choose how your positions get in

**A. tastytrade (recommended).** Live positions, streaming quotes, fills and an automatic journal.

1. On my.tastytrade.com open **Manage → API Access → OAuth Applications** and create an application. Keep the *client secret*.
2. **Manage → Create Grant** with all scopes, and copy the *refresh token*.
3. Paste both under **Broker & trades** and press **Test connection**. Add the account number only if you have more than one account.

**B. Free (manual) mode.** No broker connection; works with any broker.

1. Under **Broker & trades**, upload a CSV of your trades, or add trades one by one with the form. Download the template from the same section.
2. Stocks and options are both supported. Broker exports (including tastytrade's) are usually recognized as they are; the [CSV section of the README](../README.md#-manual-trades-the-csv) explains the columns.
3. Enter your real starting cash; otherwise ZEUS assumes the smallest account that could have funded your history.

Option quotes in free mode come from Cboe's delayed feed (about 15 minutes late) and stock quotes from Yahoo Finance, with Finviz as a fallback.

### News sources

- The free wire (Finviz, WSJ, MarketWatch) is on from the start. You can turn single sources off.
- **Benzinga Pro** *(optional)*: paste the cookies of your logged-in Benzinga Pro session. That adds the live Benzinga wire, pre-market notes and corporate events. A Benzinga data API key adds the signals feed.
- **Finviz Elite** *(optional)*: your token switches Finviz news to the official export.
- **Headline translation**: Google (rotates Google, Bing and local engines), Bing, AI, or off. The 🌐 button in the top bar shows which engine is working.

### AI (optional)

Pick a provider: **Claude Code CLI** (uses your Claude subscription), an **Anthropic API key**, or any **OpenAI-compatible** endpoint such as OpenAI, DeepSeek, OpenRouter or a local Ollama. Then choose a fast model and a smart model, and whether scheduled AI jobs run (morning news summary, market judgement, AI picks, position review). Without AI, the AI buttons simply stay hidden. The **AI** button in the top bar pauses the scheduled jobs.

### Notifications

- **Desktop notifications (macOS):** Settings → General. They fire even when the browser is closed. Allow notifications for your terminal app in macOS System Settings if they don't show.
- **Browser notifications:** Positions → 🔔 → **Enable Notifications**.
- **E-mail:** Settings → E-mail alerts. Fill in the sender, the recipients, an app password (Gmail: Google Account → Security → App passwords) and the SMTP server and port (587 or 465). Press **Save**, then **Send a test e-mail**. From then on every desktop alert also arrives by mail; on Linux this is how alerts reach you. Not available on Windows yet.
- **🔔 in the top bar** mutes everything for a while.

### Language

Use the menu in the top bar: **中文**, **双语** (Chinese with English beside every label) or **English**. News stays in English with a Chinese translation under each headline; AI answers follow your language unless you set one in Settings → AI.

## 3. A trading day with ZEUS

**Before the open**
1. **📋 Digest → 📡 Today.** Turn on **⭐ must-read** for strong catalysts and big macro news, or press **🤖 AI summary** for a two-minute read. **💼 Positions news** shows only what touches your holdings.
2. **📅 Events.** Check today's earnings and macro releases, and open **📡 Risk radar** for today's news risk and the next 30 days.

**Check your book**
3. **Positions.** Read the status lights: 🔵 take profit, 🟡 near target, 🟢 hold, 🔴 deep loss. Click a light to show only those positions.
4. Click a position for its **P/L table**, graph and Greeks. Use **⚠** for the stress test of the whole book, the expiry ladder and scenarios.

**Looking for a trade**
5. **🎯 Picker.** Scan the K-line wall, sort by market cap, today's change or TA score, and filter by setup or earnings date. **🚀 AI picks** lists trend and dip candidates with strikes and orders you can paste.
6. Click a ticker for its chart, fundamentals, news, bull and bear narratives and company profile.
7. The **TA screener** (`/ta/`) shows each signal with a recommended spread.

**Before you open it**
8. Run the **🩺 pre-trade checkup** from Positions: trend, support and resistance, IV and earnings, macro calendar. Enter the spread you plan to trade to check its price and reward to risk, and open **🌊 Liquidity** to compare strikes and weeks.
9. Not sure yet? Try it in **🧪 Paper Trade** first (section 4).

**After you trade**
10. With tastytrade, fills and the journal update by themselves. In free mode, add the trade under Settings → Broker & trades.
11. Write down why in **Trade Journal → 📋 Decisions**: thesis, confidence, emotion, signal source. ZEUS reminds you if you skip one.

**Once a week**
12. **Trade Journal → 🔍 Analysis** and **✦ AI review**: which setups pay, which mistakes repeat. Then fill in the weekly review.

## 4. Paper Trade step by step

1. Open **🧪 Paper Trade** and press **+ New Trade**.
2. Type a ticker and press Enter (or **Load**). ZEUS picks an expiration about 25–50 days out and starts with a bull call spread.
3. Change the strategy from the menu at the top left (23 templates), or build your own:
   - **Add +** → buy or sell a call or put at the money, or buy or sell 100 shares.
   - Click a dot on the strike bar to add a leg there; drag a leg's label to move it.
   - Click a leg card to change its quantity, switch call and put, or flip buy and sell. Click a price to type your own fill.
   - Pick another date on the expiration timeline; existing legs move to the nearest strikes.
4. Read the summary: net debit or credit, max loss, max profit, chance of profit, breakeven. The table and graph show P/L across prices and dates; change the range, center price or implied volatility below them.
5. Press **Save Paper Trade**. It is stored on your computer at the mid prices shown; nothing goes to a broker.
6. The **Paper Trades** list shows total return, today's return, and the open, today's and realized P/L at the top. Press **↻ Refresh P/L** to re-price.
7. Click a trade for its legs, P/L by leg, Greeks and the P/L table. **Close at market** closes it at the current prices, and its P/L moves to realized.
8. Use groups (**+** next to Group, or ⚙ → Change Group) to keep income trades and directional trades apart. **Export CSV** saves the list.

From the TA screener, **⚡** (or the **B** key on a ticker page) opens the recommended spread directly in the builder.

**How the P/L is calculated.** Option legs are valued at the broker mark (mid price); stock legs at the last price. *Today's return* compares with yesterday's close, or with the entry price for trades opened today. In free mode before the market opens, the stock price is still yesterday's close while Cboe's option quotes may already have moved, so today's return can look odd until the open.

## 5. Your data

- **Settings:** `~/.zeus-terminal/config.json` (permission 600).
- **Everything the terminal writes** (snapshots, journal, paper trades, news archives, caches, logs): `~/.zeus-terminal/runtime/`.
- **Back up** by copying `~/.zeus-terminal/`. **Start over** by deleting it.
- The code folder never holds your data, so `git pull` and `git push` cannot touch it. Move the folder with `ZEUS_HOME=/path ./start.sh`; any setting can also come from an environment variable such as `ZEUS_TT_REFRESH_TOKEN`.

## 6. Troubleshooting

| Problem | What to do |
|---|---|
| The page does not open | Look at the terminal window for errors. Is another program on port 8765? Start with `PORT=8790 ./start.sh`. |
| First launch after the close, the picker shows 0 quotes | Press **Refresh** in the picker; the last session's closing prices fill in within seconds. |
| Quotes stop updating in free mode | Yahoo Finance is throttling; ZEUS switches stock quotes to Finviz by itself. Wait a minute and refresh. |
| No AI buttons | Set an AI provider in ⚙ Settings → AI, then reload the page. |
| Benzinga news stopped | The Benzinga cookies expired. Paste fresh cookies in Settings → News sources. |
| No desktop notifications | macOS only. Turn them on in Settings → General, and allow notifications for your terminal app in macOS System Settings. |
| The test e-mail fails | Use an app password, not your normal one, and check the server and port (587 with STARTTLS or 465 with SSL). The message next to the button shows what the mail server answered. |
| The page looks out of date after an update | Reload with Cmd+Shift+R (Ctrl+Shift+R on Windows); the installed app caches pages. |
| Paper Trade says "No market data yet" | The terminal is still loading after start; wait a few seconds. If it stays, look at the terminal window: with tastytrade, the connection may have failed (press Test connection in Settings). |

Still stuck? [Open an issue](https://github.com/jackson-cmd/zeus-options-terminal/issues) with what you did and what you saw (leave out tokens and account numbers).
