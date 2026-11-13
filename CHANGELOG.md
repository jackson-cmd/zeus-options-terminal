# Changelog

[中文 → CHANGELOG_zh.md](CHANGELOG_zh.md) · [User manual](docs/USER_GUIDE.md) · [Back to README](README.md)

What changed in each version of ZEUS Terminal. New features carry the <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> badge. Numbers in parentheses are the pull requests.

## 0.4.1 — 2026-11-12

### New features

| | Feature | Read more |
|---|---|---|
| <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> | **More ? cards**: the ⚠ risk view (stress columns and totals, upside, Captured and → Max profit, portfolio Greeks, expiration ladder, Theta runway, Monte Carlo), the 🌊 liquidity scan (the 🟢 🟡 🔴 rules and ★, the cross-week table), the 📡 risk radar (today's news risk, the 30-day event density, the landmine lifts), and on the TA screener the earnings column and the key-levels panel. Each card quotes the formulas and thresholds of the code that computes the number. While the risk view, the radar or the liquidity scan is open, the top bar's ? lists its cards. (#34) | [Manual §3.1](docs/USER_GUIDE.md#31-help-on-every-page) |

### Fixed

- Bilingual mode: a column header with a ? icon lost part of its English label, so "Signal win rate (SSP/MACD/RSI/Vol)" read "Signal win rate". The label now leaves the icon out. (#34)
- The manual and the feature list said the TA screener suggests a bear put spread for bearish signals. It suggests bull call spreads only and shows the direction for bearish tickers; both now say so. (#34)

### Project

- `tests/test_help_numbers.py` compares the numbers the new cards quote with the analyzer, `checkup/risk_radar.py`, `checkup/liquidity.py` and `ta_screener/engine.py`, checks which page the guide lists while an overlay is open, and checks a bilingual header label with an icon inside. (#34)

## 0.4.0 — 2026-11-12

### New features

| | Feature | Read more |
|---|---|---|
| <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> | **❓ Help on every page**: a small ? beside the columns, scores and cards opens a card that says what the item is, how it is calculated and how to use it, written in Chinese and English. The ? in the top bar (a round ? on the TA screener, the pre-trade checkup and the journal review) lists every button and number on the page you are on, with search and **Show me on the page**. Press ? to open it. (#32) | [Manual §3.1](docs/USER_GUIDE.md#31-help-on-every-page) |

### Fixed

- TA screener: the help behind its ? icons was in Chinese only and partly out of date; it still described option and order-flow points that the score no longer counts. Its ? icons now open the new cards in the interface language, checked against the scoring code, and the score column has a ? of its own. (#32)
- Top bar: in bilingual mode the tabs sit a little closer below 1240 px, so all six still fit next to the new ? button. (#32)

## 0.3.1 — 2026-11-12

### Fixed

- Top bar: the ◐ Manual badge covered 🧪 Paper Trade in narrower windows. It is gone from the top bar; the mode and the number of manual trades are on the first card in ⚙ Settings and in the ⚙ tooltip. Below 1240 px the tabs sit closer together, so all six fit from 1024 px up in every language. (#26)
- Picker → 🧭 Market Brief: the empty brief pointed to a 🔄 Re-analyze button that was not there. The brief's header (🔄 Re-analyze and the date) and its sub-tabs 🧭 Market brief · 🔄 Sector rotation · 🎲 Options sentiment now show in the Picker, as the [manual](docs/USER_GUIDE.md#65-market-brief) describes. (#26)
- Sector rotation chart: labels no longer print over each other in a narrow window (every second or third bar is labelled instead), English group names are shortened to fit instead of running off the chart, the first labels stay inside it, and the ±% scale moved to the left margin, where the bars cannot cover it. (#26)
- The movers notice in the top right corner no longer blocks clicks on the buttons under it while it is on screen. (#26)
- The movers notice moved from the top right corner, where it covered the Market Brief's 🔄 Re-analyze button for a few seconds, to the bottom of the window. When it opened the chart wall while 🧭 Market Brief was showing, the two were stacked on one page; the Picker now switches over to the chart wall completely. (#28)
- English interface: many sentences came out as word salad, such as "future30-day event density", "Chip leader's earnings report=Sector level landmine:bad earnings report day…" or "Recently≥2%down". They now read as whole English sentences in the 📡 risk radar and its data panels, the 🩺 checkup, the liquidity scan, the positions risk view (portfolio Greeks, Monte Carlo, Theta runway), exit advice and take-profit alerts, the 🔔 alert settings, the digest status lines, sector rotation, the picker's controls, risk map and ETF trends, the trade journal's analysis cards and chart notes, the daily review page, the pre-trade checkup page and the TA page. (#30)
- Bilingual mode: labels keep their numbers and Latin words ("7 days", "Hide ETF groups") instead of dropping them, and labels such as "💹 Option volume stability · Not crawled yet" keep their separators. (#30)
- Tickers and names inside translated text are no longer split ("SOXX", "MarketWatch"). (#30)

### Project

- `static/zeus_lang.js` gives page scripts `zt(zh, en)` and `ztt(zh, en)` before they run, so a sentence built with numbers carries its English where it is built; the server sends English next to Chinese where it builds such sentences (`label_en`, `reasons_en`, `insight_en`, `headline_en`…). The dictionary lookup in `zeus.js` finds a sentence through line breaks, emoji and colons around it, and translates the parts of a line joined by " · " or " — " one by one. About 350 sentences were added to `i18n_en.json` and 24 wrong ones corrected ("流动性体检" read "Mobile medical check-up", "出场" read "Appearance"). (#30)
- `tests/test_english_text.py` keeps the page text, the dictionary and the English templates in step, and runs the lookup code of `zeus.js` under node. (#30)

## 0.3.0 — 2026-11-12

### New features

| | Feature | Read more |
|---|---|---|
| <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> | **🔌 AI agents (MCP)**: ZEUS is an MCP server at `http://127.0.0.1:8765/mcp`. Claude Code, Claude Desktop, Codex CLI (GPT), Gemini CLI and agents you build get 18 tools: positions with ZEUS's own P/L, max profit and loss, breakevens, chance of profit, Greeks, stress and exit advice; what-if analysis of any strategy priced from the live chain; the pre-trade checkup; spread suggestions; liquidity; quotes and option chains; market overview; events; news; sector rotation; trade ideas; ticker reports; the trade journal; and Paper Trade, where opening and closing need your permission. There are also four prompts: portfolio review, pre-trade check, morning brief and journal coach. It speaks the current stateless protocol (2026-07-28) and the earlier handshake-based revisions, answers only programs on this computer, and can never place a real order. (#24) | [Manual §18](docs/USER_GUIDE.md#18-ai-agents-mcp) |
| <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> | **⚙ Settings → AI agents (MCP)**: switch the server on or off, allow paper trading by agents, copy the setup for each agent, and test the connection. (#24) | [Manual §18.2](docs/USER_GUIDE.md#182-connect-your-agent) |
| <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> | **`zeus_mcp_stdio.py`**: a bridge for clients that launch a command (Claude Desktop). It needs only the Python standard library; `--check` tests the connection. (#24) | [Manual §18.2](docs/USER_GUIDE.md#182-connect-your-agent) |

### Project

- `zeus_payoff.py` ports the page's option math to Python (max profit and loss, breakevens, chance of profit, Greeks, stress and VaR). Tests run it and the page's JavaScript on the same strategies and require the same numbers. (#24)
- 65 new tests: the MCP protocol in both eras, the security checks, every tool against a fake terminal, and the stdio bridge end to end through a real HTTP server. The official MCP TypeScript and Python SDKs were checked against a running terminal over HTTP and stdio. (#24)

## 0.2.1 — 2026-11-12

### New features

| | Feature | Read more |
|---|---|---|
| <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> | **🟩 Heatmap back in the Picker**: the universe as tiles by sector and industry, area = market cap, color = today's change; one click switches between it and the K-line chart wall. (#23) | [Manual §6.9](docs/USER_GUIDE.md#69-heatmap) |

### Fixed

- Top bar: in English and bilingual mode the tab labels wrapped onto two or three lines and were cut off; every label now stays on one line, and in a narrow window the tabs scroll sideways. (#23)
- Positions toolbar: the 🤖 AI and 🔥/💲 buttons no longer break into two lines. (#23)
- Fonts: without Roboto (on Linux, for example) the interface fell back to a monospace font; it now falls back to the system sans-serif and Chinese fonts. (#23)
- Picker: the K-line chart wall could stay at "0 tickers" when it was drawn before the quotes arrived. (#23)
- Picker: the 📊 Barchart menu ran off the right edge of the window, and the button showed even without a Barchart login; it now appears once a login is saved. (#23)
- Events: the earnings calendar was empty. It read the watchlist as one long ticker, and Nasdaq refuses the request on many networks; it now reads the list correctly and falls back to the earnings dates the picker keeps. The pre-trade checkup's earnings gate uses the same dates and finds them again. (#23)
- Events: macro events in the calendar were cut down to "⚡ 08…"; the months now sit three, two or one to a row depending on the window, and event names come before the time. (#23)
- Pop-ups close with **Esc**, and switching to another tab closes them, instead of leaving the risk radar over the next page. (#23)
- After an update, browsers could keep the old interface scripts for a while; they are now checked on every load. (#23)
- Garbled English in a few places: the chart wall legend, the daily review headings, the checkup page subtitle. (#23)

## 0.2.0 — 2026-11-12

### New features

| | Feature | Read more |
|---|---|---|
| <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> | **🧪 Paper Trade**: an OptionStrat-style builder on the live option chain with option and stock legs and 23 strategy templates. Save simulated trades and follow their total, today's and realized P/L; close at market; groups and CSV export. The ⚡ button in the TA screener opens recommended spreads here. Nothing is ever sent to a broker. (#5) | [Manual §8](docs/USER_GUIDE.md#8-paper-trade) |
| <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> | **📧 E-mail alerts**: every desktop alert (take-profit, missing GTC, risk radar, support / resistance, AI picks…) can also arrive by e-mail, on macOS and on Linux. Repeats within 30 minutes are skipped, at most 20 per hour, with a test button in Settings. (#14) | [Manual §12.3](docs/USER_GUIDE.md#123-e-mail-alerts) |
| <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> | **🔄 Sector rotation** and **🎲 options sentiment** work out of the box: the industry universe (80 groups, 575 tickers) now ships with ZEUS, with English group names. (#13) | [Manual §6.6](docs/USER_GUIDE.md#66-sector-rotation) |
| <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> | **∞ Exact max profit and loss**: short puts, long puts and stock are evaluated down to a price of zero, and unlimited strategies show ∞ instead of a made-up number. (#11) | [Manual §4.7](docs/USER_GUIDE.md#47-how-positions-are-grouped-and-priced) |
| <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> | **🌐 English TA screener and single-ticker page**: they follow 中文 / 双语 / English like the rest of the terminal. (#13) | [Manual §13](docs/USER_GUIDE.md#13-interface-languages) |
| <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> | **📚 Complete user manual and feature checklist** in English and Chinese, with clickable contents. (#6, #15) | [Manual](docs/USER_GUIDE.md) · [Checklist](docs/FEATURES.md) |

### Fixed

- Positions: the max loss of short puts and the max profit of long puts were understated, because prices were only checked down to half the lowest strike. (#11)
- Manual mode: the previous close came from a Yahoo field that can be days old, so the day change and today's P/L could be wrong. (#5)
- With tastytrade connected, the signal backtest job downloaded 264 option chains every weekday for a page that does not exist. It is no longer started. (#12)
- The trade journal iframe now shows bilingual labels. (#13)
- Positions: a 🩺 button opens the checkup panel, which used to open only from the red alert bar. (#18)
- The four-gate pre-trade checkup page and the daily review page now follow the interface language. (#18)

### Project

- Every pull request runs the tests on Linux, macOS and Windows (7 jobs), and `main` only accepts pull requests whose checks pass. (#4)
- Contributing guide, issue templates and a pull request template. (#6)

## 0.1.0 — 2026-11-12

First public release: positions with live P/L and Greeks, the exit advisor and pre-trade checkup, the picker with AI picks, the four-source news digest, earnings and SEC filings, the trade journal with the decision log, three interface languages, and free manual mode with Cboe, Yahoo and Finviz data.
