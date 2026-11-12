# 更新记录

[English → CHANGELOG.md](CHANGELOG.md) · [使用手册](docs/USER_GUIDE_zh.md) · [回到 README](README_zh.md)

ZEUS Terminal 每个版本改了什么。新功能前面挂着 <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> 标记，括号里的数字是对应的 PR 编号。

## 0.2.0 —— 2026-11-12

### 新功能

| | 功能 | 详细说明 |
|---|---|---|
| <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> | **🧪 模拟盘**：在实时期权链上搭策略，用法和 OptionStrat 一样，期权腿和股票腿都能加，有 23 个策略模板。存成模拟单后跟踪总收益、今日收益和已实现盈亏；可以按市价平仓、分组、导出 CSV。TA 扫描里的 ⚡ 按钮能把推荐价差直接载入。任何单子都不会发到券商。（#5） | [手册 §8](docs/USER_GUIDE_zh.md#8-模拟盘) |
| <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> | **📧 邮件提醒**：每条桌面提醒（止盈、漏挂 GTC、风险雷达、支撑阻力、AI 选股……）都能同时发到邮箱，macOS 和 Linux 都支持。同一条提醒 30 分钟内只发一次，每小时最多 20 封，设置页有测试按钮。（#14） | [手册 §12.3](docs/USER_GUIDE_zh.md#123-邮件提醒) |
| <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> | **🔄 行业轮动** 和 **🎲 期权情绪** 开箱即用：行业票池（80 组、575 只）现在随仓库提供，分组名配了英文。（#13） | [手册 §6.6](docs/USER_GUIDE_zh.md#66-行业轮动) |
| <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> | **∞ 精确的最大盈亏**：卖出看跌、买入看跌和股票都算到价格为 0 的情况，没有上限的策略显示 ∞，不再显示一个凑出来的数。（#11） | [手册 §4.7](docs/USER_GUIDE_zh.md#47-持仓怎么归组和估值) |
| <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> | **🌐 TA 扫描和单票详情页英文化**：和终端其他页面一样跟着中文 / 双语 / English 切换。（#13） | [手册 §13](docs/USER_GUIDE_zh.md#13-界面语言) |
| <img src="https://img.shields.io/badge/NEW!-e5534b" alt="NEW!"> | **📚 完整使用手册和功能清单**，中英文各一份，目录可以点击跳转。（#6、#15） | [手册](docs/USER_GUIDE_zh.md) · [清单](docs/FEATURES_zh.md) |

### 修复

- 持仓页：卖出看跌的最大亏损、买入看跌的最大盈利原来会算小，因为只算到最低行权价的一半。（#11）
- 手动模式：昨收价取自 Yahoo 一个可能是几天前的字段，导致当日涨跌和今日盈亏可能不对。（#5）
- 连了 tastytrade 时，信号回测的定时任务每个工作日都抓 264 只股票的期权链，但对应的页面根本不存在。现在不再启动它。（#12）
- 交易日记内嵌页现在能显示双语标签。（#13）
- 持仓页加了 🩺 按钮打开体检面板，原来只能点红色告警条打开。（#18）
- 四道关开单前体检页和每日复盘页现在跟着界面语言切换。（#18）

### 项目

- 每个 PR 都在 Linux、macOS、Windows 上跑测试（7 个任务），`main` 只接受测试全部通过的 PR。（#4）
- 加了贡献指南、issue 模板和 PR 模板。（#6）

## 0.1.0 —— 2026-11-12

第一次公开发布：带实时盈亏和希腊值的持仓、出场顾问和开单前体检、带 AI 选股的选股页、四路新闻 Digest、财报和 SEC 文件、带决策日志的交易日记、三种界面语言，以及用 Cboe、Yahoo、Finviz 数据的免费手动模式。
