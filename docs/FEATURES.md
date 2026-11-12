# ZEUS Terminal — complete feature checklist

[中文 → FEATURES_zh.md](FEATURES_zh.md) · [User manual](USER_GUIDE.md) · [What's new](../CHANGELOG.md) · [Back to README](../README.md)

ZEUS does far more than the screenshots in the README show. There are 7 pages in the top bar, about 30 jobs that run by themselves in the background, and more than 200 API endpoints. This page lists every feature you can reach from the interface today. Pull requests are welcome, and new features may be added over time ([Contributing](../CONTRIBUTING.md)). Features marked <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> arrived in version 0.2.0 ([changelog](../CHANGELOG.md)); how to use each one is in the [user manual](USER_GUIDE.md).

**What each feature needs:**

| Tag | Meaning |
|---|---|
| Free | Works in free (manual) mode with public data |
| tastytrade | Needs a connected tastytrade account |
| Benzinga | Needs a Benzinga Pro login (cookies) |
| Benzinga API | Needs a Benzinga data API key |
| AI | Needs an AI provider (Claude Code CLI, Anthropic API, or any OpenAI-compatible endpoint) |
| macOS | Desktop notifications only work on macOS |

---

## Top bar (on every page)

| ✓ | Feature | What it does | Needs |
|---|---|---|---|
| ✅ | Page tabs | Positions · 🎯 Picker · 📋 Digest · 📅 Events · Trade Journal · 🧪 Paper Trade | Free |
| ✅ | Mode badge | Shows tastytrade or ◐ manual mode, with the number of manual trades | Free |
| ✅ | Language menu | 中文 · 双语 (bilingual) · English, switch any time | Free |
| ✅ | ⚙ Settings | Opens the settings page; everything is configured in the browser | Free |
| ✅ | AI pause | Pauses the scheduled AI jobs (picks, brief, summaries) with one click | AI |
| ✅ | 🔔 Mute | Silences every notification: take-profit, movers, risk, AI position review | Free |
| ✅ | 🌐 Translation status | Which translation engine is working now, why the others fail, and a manual test | Free |
| ✅ | 👁 Privacy blur | Blurs dollar amounts for screen sharing and recording | Free |
| ✅ | LIVE indicator | Shows whether the live data connection is up | Free |
| ✅ | Install as an app | The terminal is a PWA: install it from the browser and it opens in its own window | Free |

## ⚔️ Positions

| ✓ | Feature | What it does | Needs |
|---|---|---|---|
| ✅ | Automatic strategy grouping | Verticals, calendars, diagonals, iron condors, butterflies, naked and covered legs and stock, grouped by the order that opened them, so rolls and legging-in stay together | Free |
| ✅ | Live P/L | Every leg marked to market; today's P/L from the official previous close; total return in $ and % | Free (live streaming with tastytrade) |
| ✅ | Sortable board | Name, underlying, total return, today's return, distance to breakeven, max profit, max loss (∞ when unlimited), chance of profit, DTE, progress to max profit | Free |
| ✅ | Status lights and filter | 🔴 deep loss · 🔵 take profit · 🟡 near target · 🟢 hold; click a light to filter | Free |
| ✅ | Account bar | Net liquidating value, buying power used, cash, live tickers | Free (buying power with tastytrade) |
| ✅ | Compact · Freeze · Refresh | Denser rows; freeze the numbers while you read; refresh on demand | Free |
| ✅ | 📈 Portfolio overview | The whole book on one screen | Free |
| ✅ | 🔥 P/L heatmap / 💲 P/L bars | Every position as a heat tile or a bar, sized by P/L | Free |
| ✅ | 🤖 AI position review | AI reads each position with its news and suggests hold, trim or close; can also run on a schedule | AI |
| ✅ | 🔔 Alert panel | Recent alerts and a button to enable browser notifications | Free |

## 🔍 Strategy detail (click any position)

| ✓ | Feature | What it does | Needs |
|---|---|---|---|
| ✅ | P/L table | Profit or loss at every price and date until expiration, as $, %, contract value or % of max risk | Free |
| ✅ | P/L graph | Expiration payoff and today's curve, with a date slider | Free |
| ✅ | Controls | Price range, jump to a price, implied-volatility override and reset, midpoint or bid/ask pricing | Free |
| ✅ | Greeks | Δ Γ Θ Vega ρ per leg and in total, with a skew-aware, vega-weighted spread IV | Free |
| ✅ | TradingView chart | The underlying's chart in a pop-up | Free |
| ✅ | Earnings | Next earnings date and how the stock moved on past reports | Free |

## ⚠ Risk view

| ✓ | Feature | What it does | Needs |
|---|---|---|---|
| ✅ | Stress test | 1σ and 2σ moves, 1σ and 2σ worst case, VaR 95 %, CVaR 95 %, max loss and profit, share of profit already captured, chance of profit, distance to max profit — per position | Free |
| ✅ | 📅 Expiry ladder | What expires when, and how much theta each week earns | Free |
| ✅ | 🎯 Scenarios | Move the market and implied volatility with sliders and watch the whole book | Free |
| ✅ | 📊 P/L probabilities | Probability distribution of the book's P/L | Free |
| ✅ | 📊 Risk research notes | The rules and statistics the exit advice is built on | Free |

## 🩺 Discipline and alerts

| ✓ | Feature | What it does | Needs |
|---|---|---|---|
| ✅ | Exit advisor | Take-profit band that adapts to the market regime: about 70–80 % of max profit in a bull market, 50–60 % when choppy | Free |
| ✅ | Take-profit alerts | Notifies you when a position reaches its target | Free (desktop: macOS) |
| ✅ | Missing take-profit order alarm | Red bar when a position has no working GTC exit order | tastytrade |
| ✅ | Credit-spread mistake detector | Catches a vertical that filled as a net credit when you meant to pay a debit (a bull call spread entered as a bear call spread), once per spread | Free |
| ✅ | Support / resistance alerts | Notifies you when a stock you hold touches a support or resistance zone | Free |
| ✅ | Risk radar | 📡 today's news risk, 📊 why stocks are moving, a 30-day landmine calendar; alerts when risk spikes | Free |
| ✅ | Concentration, correlation, liquidity | Position size as % of net liquidation, correlation matrix, cash-reserve guard | Free |
| ✅ | Decision-log reminders | Reminds you to record why you opened or closed a trade | Free |
| ✅ | Desktop notifications | Everything above as macOS notifications, even with the browser closed | macOS |
| ✅ | E-mail alerts <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> | Every alert above also by e-mail; the same alert at most once per 30 minutes and at most 20 alerts per hour; test button in Settings | SMTP account (macOS, Linux) |

## 🩺 Pre-trade checkup and liquidity

| ✓ | Feature | What it does | Needs |
|---|---|---|---|
| ✅ | Four-gate pre-trade page | `/pretrade-checkup`: one ticker and direction through four gates (trend → touch of support / resistance → IV and earnings → macro calendar) to a verdict; trading against it is recorded with your reason | Free |
| ✅ | 🩺 Checkup panel | From the Positions toolbar: each candidate's trend, beta, correlation with what you hold, concentration, and a mirror of the mistakes your own journal shows | Free |
| ✅ | Spread check | Enter the spread you plan to open (or load the chain): price, direction, how far out of the money, reward to risk | Free |
| ✅ | Spread suggestions | Ranked bull call spreads by fill quality, liquidity and reward to risk, adapted to the market regime | Free |
| ✅ | Position exits | The same checks for positions you already hold: take profit or stop now? | Free |
| ✅ | 🌊 Liquidity scan | Calls and puts by strike and expiration with bid/ask width, volume and open interest, plus the same spread across weeks | Free |

## 🎯 Picker

| ✓ | Feature | What it does | Needs |
|---|---|---|---|
| ✅ | 📈 K-line chart wall | Daily candles for the whole universe with EMA 50/200, multi-timeframe support and resistance, gold zones and badges | Free |
| ✅ | Sorting and filters | By market cap, today's change or TA score; by earnings date and bullish / bearish setup | Free |
| ✅ | 🚀 AI picks | Trend-following and dip-buying candidates with strikes, chance of profit and orders you can paste; AI re-ranks them with news and earnings | Free (AI re-rank needs AI) |
| ✅ | 🧭 Market judgement | How bad the news is versus how much price has absorbed, crash probability against base rates, three layers of support, and what would prove the view wrong | AI |
| ✅ | 🔄 Sector rotation <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> | 80 industry groups (575 tickers) ranked by their moves over 5 days to a year, with each group's trend and the share above the 50- and 200-day averages | Free |
| ✅ | 🎲 Options sentiment <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> | IV percentile, 25-delta skew percentile and put/call percentiles by sector, against their own history | Free (needs a few weeks of after-close crawls) |
| ✅ | 📊 Barchart lists | Intersect or union your saved Barchart screeners and signal pages | Barchart account |
| ✅ | Ticker detail | Chart (1 week to 2 years), Finviz technicals and fundamentals, news, ⚖️ bull/bear narratives, 🏢 company profile written from the latest 10-Q, 📑 jump to SEC filings | Free (narratives and profile need AI) |
| ✅ | ⚡ After-close crawl | Pull the day's closing data for the whole universe on demand | Free |

## 🔎 TA screener (`/ta/`)

| ✓ | Feature | What it does | Needs |
|---|---|---|---|
| ✅ | Daily scan | MACD, RSI, volume, EMA breaks and support / resistance confluence over the universe, with walk-forward win rates | Free |
| ✅ | Table · Chart · Heatmap | Three views; the heatmap groups by sector or industry; sparklines over 3 months, 1 year or 2 years | Free |
| ✅ | Recommended spreads | A bull call or bear put spread per signal, with ⚠️ earnings-before-expiration flags | Free |
| ✅ | 📋 Order ticket | Copy the spread as an order ticket | Free |
| ✅ | ⚡ Paper Trade | Open the recommended spread in the Paper Trade builder (shortcut **B** on a ticker page) | Free |

## 🧪 Paper Trade <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!">

| ✓ | Feature | What it does | Needs |
|---|---|---|---|
| ✅ | Strategy builder | OptionStrat-style: expiration timeline, draggable strike bar with volume / open interest, leg cards with editable prices | Free |
| ✅ | 23 templates | Long call and put, covered call, cash-secured put, protective put, bull and bear verticals (debit and credit), iron condor, iron butterfly, call and put butterflies, straddles, strangles, short naked options, ratio backspreads, jade lizard and reverse jade lizard | Free |
| ✅ | Stock legs | Buy or sell shares next to options: covered calls, protective puts, collars | Free |
| ✅ | Analysis before saving | Net debit or credit, exact max profit and loss (∞ when unlimited), breakeven, chance of profit, Greeks, P/L table and graph with range, price and IV controls | Free |
| ✅ | Save Paper Trade | Records a simulated fill at the mid price; never sent to a broker | Free |
| ✅ | Live P/L | Open trades marked to market: total return, today's return, open / today / realized summary | Free |
| ✅ | Manage | Groups, sorting, filter open / closed, close at market, delete, CSV export | Free |

## 📋 Digest

| ✓ | Feature | What it does | Needs |
|---|---|---|---|
| ✅ | 📡 Today | Live news list, refreshed every five minutes | Free |
| ✅ | Free bilingual wire | Finviz, WSJ (Markets, Business, Tech, World) and MarketWatch, every headline translated into Chinese | Free |
| ✅ | Benzinga wire | Benzinga's live news pushed in real time, with its categories | Benzinga |
| ✅ | 🌅 Pre-market | Benzinga's pre-market commentary and options-flow reports | Benzinga |
| ✅ | ✦AI | Finviz's "why is it moving" notes for your universe | Free |
| ✅ | Filters | ⭐ must-read, sentiment ▲ ▼ ●, ticker, 🔖 watchlist only, ✨ de-noise, source chips, categories | Free |
| ✅ | Reading tools | Mark all read, show read items, expand / collapse all | Free |
| ✅ | 🤖 AI summary | The day (or any filter) merged into a two-minute read, updated as news lands; a morning summary is made automatically | AI |
| ✅ | 📊 Tickers | News counts per ticker over 1, 3, 7 or 30 days, with prices | Free |
| ✅ | 💼 Positions news | Only the news about what you hold | Free |
| ✅ | 📅 Calendar / history | Any past day, presets for 7 days, 1 month, 3 months, and full-text search | Free |
| ✅ | ❤️ Liked | Stories you liked, kept across days | Free |
| ✅ | 📈 Market chart | SPY and QQQ full-history candles with major turning points; click one and AI explains the news behind it | Free (explanations need AI) |

## 📅 Events

| ✓ | Feature | What it does | Needs |
|---|---|---|---|
| ✅ | 📅 Earnings calendar | Your universe and positions, before / after market, plus high-impact macro dates | Free |
| ✅ | 📋 Corporate events | IPOs, M&A, secondary offerings, splits | Benzinga |
| ✅ | 📄 SEC filings reader | 10-K, 10-Q and 8-K straight from EDGAR, with a table of contents | Free |
| ✅ | Bilingual filings | English and Chinese side by side | Free |
| ✅ | 📞 Earnings-call transcripts | From public call recordings | Free (needs `yt-dlp`) |
| ✅ | 📋 Company profile | A summary of the business written from the filing | AI |
| ✅ | 🗂 Filing cache | Filings you already opened, available offline | Free |
| ✅ | 📡 Risk radar | Also reachable from here | Free |

## 📓 Trade journal

| ✓ | Feature | What it does | Needs |
|---|---|---|---|
| ✅ | 📊 Overview | Win rate, profit factor, average trade, max drawdown, closed vs open P/L, fees, equity curve | Free |
| ✅ | Monthly targets | Set your own targets (⚙ targets) and track them | Free |
| ✅ | 📅 Calendar | P/L by day | Free |
| ✅ | 📋 All trades | Every leg and every fill | Free |
| ✅ | 🔍 Analysis | P/L by symbol, holding time and setup; distributions; cumulative curves; filters | Free |
| ✅ | What-if engine | Would taking profit earlier, later or at expiration have paid, on your own trades? | Free |
| ✅ | ✦ AI review | An AI coach reads your trades and notes and names the mistakes you repeat | AI |
| ✅ | 📋 Decisions | A decision log: thesis, confidence, emotion and signal source for every open and close, from templates | Free |
| ✅ | Price path after the decision | Spread value, IV and Greeks after you acted, as a chart or table | Free |
| ✅ | 📸 Screenshots | Capture a chart into the decision, or attach images | Free |
| ✅ | Weekly review | A weekly review of closed trades, saved by week, with a reminder | Free |
| ✅ | Daily review page | `/journal-review`: yesterday's opened and closed trades, rule checks, today's watchlist | Free |
| ✅ | Sync | Hourly from tastytrade, or rebuilt instantly from your uploads | Free (sync with tastytrade) |

## ⚙ Settings

| ✓ | Group | What you can set |
|---|---|---|
| ✅ | Broker & trades | tastytrade client ID, client secret, refresh token, account number; or manual trades (CSV upload or form) and starting cash; connection test |
| ✅ | News sources | Benzinga cookies, Benzinga data API key, Benzinga login, Finviz Elite token, which free sources to use, headline translation (Google / Bing / AI / off), Google translate key |
| ✅ | AI | Provider, Claude CLI path, Anthropic or OpenAI-compatible key and URL, fast and smart models, answer language, scheduled AI jobs on / off |
| ✅ | Other data | Barchart login, Finviz "why is it moving" crawl on / off, SEC EDGAR User-Agent |
| ✅ | E-mail alerts | From, to, app password, SMTP server and port; a button sends a test e-mail |
| ✅ | General | Language, listen address, port, desktop notifications |

Saved secrets are write-only: the page shows *set* or *not set* and never sends a secret back.

## 🔁 What runs by itself

| ✓ | Job | When |
|---|---|---|
| ✅ | Positions, marks, stock prices, balances | Every few seconds to minutes while the terminal runs; streamed with tastytrade |
| ✅ | Free news wire and Digest live list | Every five minutes |
| ✅ | Benzinga daily refresh, pre-market | Daily (Benzinga) |
| ✅ | Finviz "why is it moving" crawl | Slowly, about one request per second |
| ✅ | AI morning summary, market judgement, AI picks, position review | On schedule, if AI and scheduled jobs are on |
| ✅ | TA scan | Four times a trading day: 09:40, 10:30, 12:00 and 15:00 New York time |
| ✅ | Unusual options activity archive | Every 15 minutes in market hours (Benzinga) |
| ✅ | Earnings and macro calendar | Daily |
| ✅ | Journal sync | Hourly (tastytrade) |
| ✅ | Alerts: take-profit, missing GTC, credit-spread mistakes, support / resistance, risk radar, decision-log reminders, weekly review (also by e-mail when set up) | Continuously |
| ✅ | Bilingual news notification | Daily |

## 🌍 Data sources

| Source | Used for | Cost |
|---|---|---|
| tastytrade REST + DXLink | Positions, fills, balances, orders, real-time quotes and Greeks | Free with an account |
| Cboe delayed quotes | Option chains, bid/ask, IV and Greeks in manual mode (about 15 minutes delayed) | Free |
| Yahoo Finance | Stock quotes and daily candles | Free |
| Finviz | Quotes when Yahoo throttles, company data in batches, news, "why is it moving" | Free (Elite token optional) |
| WSJ and MarketWatch RSS | News | Free |
| SEC EDGAR | Filings | Free |
| Benzinga Pro | Live wire, pre-market, events, signals | Subscription |
| Barchart | Screener and signal lists | Account |
| Google / Bing / local engines | Headline translation | Free |

## 🛠 For developers

| ✓ | Feature | Notes |
|---|---|---|
| ✅ | REST API | More than 200 endpoints under `/api/...`, for example `/api/paper-trade`, `/api/digest`, `/api/checkup`, `/api/sec` |
| ✅ | Configuration by environment | Any setting as `ZEUS_<KEY>`; move the settings folder with `ZEUS_HOME` |
| ✅ | Tests | `python -m pytest` (offline; quotes are faked) |
| ✅ | CI | Every pull request runs the tests on Linux, macOS and Windows (7 jobs); `main` only accepts green pull requests |
| ✅ | Privacy hook | `tools/install_git_hook.sh` refuses data files, keys and your own paths in commits |
| ✅ | Clean export | `tools/export_clean_zip.sh` builds a zip of the code only |

---

## Not wired up yet — ideas for pull requests

These parts exist in the code but are hidden or incomplete in the public version. They are good places to start a pull request.

- **Unusual options activity** sub-tab in Events: the background fetch exists; the tab is hidden.
- **More picker data modes**: ETF trends, beta and correlation, movers lists and option volume are hidden in the picker toolbar.
- **Signal backtest**: an automatic paper backtest of screener signals (stop-loss / take-profit modes). It needs SSP1 screener signals and a page, so its daily job is off.
- **History news search panel**: the search API works; the panel has no entry point.
- **Alerts on Windows**: desktop notifications are macOS only and e-mail alerts run on macOS and Linux; Windows has neither yet.
- **More brokers** for live positions: today only tastytrade connects live; any other broker works through CSV.
