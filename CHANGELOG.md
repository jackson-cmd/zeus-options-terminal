# Changelog

[中文 → CHANGELOG_zh.md](CHANGELOG_zh.md) · [User manual](docs/USER_GUIDE.md) · [Back to README](README.md)

What changed in each version of ZEUS Terminal. New features carry the <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> badge. Numbers in parentheses are the pull requests.

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

### Project

- Every pull request runs the tests on Linux, macOS and Windows (7 jobs), and `main` only accepts pull requests whose checks pass. (#4)
- Contributing guide, issue templates and a pull request template. (#6)

## 0.1.0 — 2026-11-12

First public release: positions with live P/L and Greeks, the exit advisor and pre-trade checkup, the picker with AI picks, the four-source news digest, earnings and SEC filings, the trade journal with the decision log, three interface languages, and free manual mode with Cboe, Yahoo and Finviz data.
