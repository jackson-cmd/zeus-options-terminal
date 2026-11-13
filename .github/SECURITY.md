# Security policy · 安全政策

[English](#english) · [中文](#中文)

---

## English

ZEUS runs on your own computer and keeps the keys to your broker accounts, so reports about its security are very welcome.

### Reporting a problem

Please report it privately, not in a public issue: open the **Security** tab of this repository and click **Report a vulnerability** ([direct link](https://github.com/jackson-cmd/zeus-options-terminal/security/advisories/new)). Only the maintainer can read the report.

Tell us what an attacker can do and how, with steps or a small proof of concept, and the version you run (`git describe --tags` in the ZEUS folder, or `APP_VERSION` in `zeus/zeus_config.py`). Leave out your real tokens, keys and account numbers.

You will get an answer on the report. Confirmed problems are fixed in a new release, and the advisory credits you unless you would rather stay unnamed.

### Supported versions

Fixes go into the latest release only, so please check that the problem is still there after `git pull`.

### What we want to hear about

- A way to read the broker tokens, API keys, OAuth credentials or e-mail passwords that ZEUS keeps in `~/.zeus-terminal`, or to get them out through the settings API.
- A way for another device on your network, or a web page open in your browser, to reach the ZEUS server or its MCP endpoint: to read your positions, change settings or start an action.
- Any way to make ZEUS send an order to a broker. ZEUS only reads from brokers; paper trades stay inside ZEUS.
- Secrets or personal data that end up in the repository, in a zip made by `tools/export_clean_zip.sh`, in a log or in an error message.

Problems in tastytrade, Alpaca, Public or the other data sources themselves belong with those companies. Attacks that start with someone who already controls your computer are out of scope.

---

## 中文

ZEUS 跑在你自己的电脑上，存着你券商账户的钥匙，所以非常欢迎报告安全问题。

### 怎么报告

请私下报告，不要开公开的 Issue：打开本仓库的 **Security** 标签页，点 **Report a vulnerability**（[直达链接](https://github.com/jackson-cmd/zeus-options-terminal/security/advisories/new)）。报告只有维护者能看到。

写清楚攻击者能做什么、怎么做到，附上步骤或一小段验证代码，以及你用的版本（在 ZEUS 文件夹里运行 `git describe --tags`，或者看 `zeus/zeus_config.py` 里的 `APP_VERSION`）。不要附上真实的 token、密钥和账户号。

我们会在报告里回复。确认的问题会在新版本里修好，安全公告里会写上你的名字，除非你不想署名。

### 支持的版本

只有最新版会修，所以报告前请先 `git pull`，确认问题还在。

### 我们想知道的问题

- 能读到 ZEUS 存在 `~/.zeus-terminal` 里的券商 token、API key、OAuth 凭证或邮箱密码，或者能通过设置接口把它们拿出去。
- 你局域网里的别的设备，或者你浏览器里打开的某个网页，能连到 ZEUS 服务器或它的 MCP 接口：读到你的持仓、改设置、触发操作。
- 任何能让 ZEUS 向券商发出订单的办法。ZEUS 对券商只读，模拟盘的交易只留在 ZEUS 里。
- 秘密或个人数据出现在仓库里、`tools/export_clean_zip.sh` 打出来的压缩包里、日志或报错信息里。

tastytrade、Alpaca、Public 和其他数据源本身的问题，请直接报给这些公司。前提是别人已经控制了你电脑的攻击，不在范围内。
