# Contributing to ZEUS Terminal · 参与贡献

[English](#english) · [中文](#中文)

---

## English

ZEUS has far more features than the README can show, and it keeps growing. Pull requests are welcome: bug fixes, new data sources or brokers, translations, docs, and the parts listed under [Not wired up yet](../docs/FEATURES.md#not-wired-up-yet--ideas-for-pull-requests).

### Sending a pull request

1. For anything bigger than a small fix, open an issue first so we can agree on the approach.
2. Fork the repository and create a branch from `main`.
3. Set up and run the tests. They run offline; quotes are faked.
   ```bash
   python -m pip install -r requirements.txt pytest
   python -m pytest
   ```
4. Install the privacy hook once: `tools/install_git_hook.sh`. It refuses data files, keys and your own paths in a commit. A pre-commit hook you already had keeps running after it.
5. Open a pull request against `main`. CI runs the tests on Linux, macOS and Windows (7 jobs), then builds the pip package and starts `zeus` from it (1 job); a pull request can merge only when all 8 pass. `main` only accepts changes through pull requests.

### Releasing a new version

A merge that raises the version publishes the release by itself. In the pull request, raise `APP_VERSION` in `zeus/zeus_config.py` and the three versions in `server.json` (the server, the PyPI package and the image tag), and add a section for the new version at the top of both `CHANGELOG.md` and `CHANGELOG_zh.md`. After the merge, the Release workflow tags the commit `v<version>` and publishes a GitHub release whose notes are those two sections. `tests/test_release.py` fails when the version and the two changelogs disagree. The same run starts the Docker image build and, when the repository variable `PYPI_PUBLISH` is `true`, uploads the release to PyPI as `zeus-options-terminal` after `tools/package_smoke.sh` has installed and started the wheel. Once the image is pushed, the Docker workflow lists the version in the official MCP Registry as `io.github.jackson-cmd/zeus-options-terminal` (`server.json`); `tests/test_mcp_registry.py` fails when `server.json` and `APP_VERSION` disagree. Changes that users do not notice, such as docs and tests, keep the version.

### Where things are

All code lives in `zeus/`; the top of the repository holds the docs, website, tests and start scripts.

```
zeus-options-terminal/
├── zeus/                 all the code
│   ├── options_analyzer/   the server (optionstrat_server.py), positions, checkup, paper trade, page assets
│   ├── heatmap/ · ta_screener/ · ai_picks/ · stock_selector/ · barchart/      picker and screeners
│   ├── benzinga_digest/ · news_radar/ · market_brief/ · sec_filings/ …        news, brief, events
│   ├── trade_journal/ · journal_review/ · journal_expiry_whatif/              the journal
│   ├── zeus_*.py · local_broker.py    settings, brokers (Alpaca, Public), AI, news, MCP, e-mail
│   ├── Dockerfile          the container image on ghcr.io (and Dockerfile.dockerignore)
│   └── defaults/           ticker lists seeded on the first start
├── docs/                 user manual, feature list, screenshots, README images
├── site/                 the project website (GitHub Pages)
├── tests/                offline tests: python -m pytest
├── tools/                privacy pre-commit hook, clean zip export, release notes, checks of the Docker image and the pip package
├── examples/             CSV template for manual trades
├── .github/              CI and release workflows, issue forms, CONTRIBUTING, security policy, code of conduct
├── server.json · glama.json · CITATION.cff   entries for the MCP Registry, Glama and Zenodo
└── start.sh · start.command · start.bat · requirements.txt · pyproject.toml
```

### Guidelines

- Never commit credentials, account numbers, real trades or anything from `~/.zeus-terminal/`.
- Keep a new feature in its own module: a router for the API and a JS string or static file for the page, wired into the server in a few lines. `zeus/options_analyzer/paper_trade_routes.py` and `zeus/options_analyzer/paper_trade.py` are a small example.
- Add tests for new endpoints, with faked market data as in `tests/test_paper_trade.py`.
- Update user-facing text in both languages: `README.md` and `README_zh.md`, `docs/*`, and `zeus/options_analyzer/static/i18n_en.json` for new Chinese interface strings.
- Add each Chinese sentence to `i18n_en.json` whole, never piece by piece. A sentence built with numbers cannot be a dictionary key, so give its English where it is built: `zt('剩 ' + n + ' 天', n + ' days left')` in a page script (`ztt` for a tooltip), or an `_en` field next to the Chinese in data the server sends. `tests/test_english_text.py` checks both.
- Be kind: the [code of conduct](CODE_OF_CONDUCT.md) applies in issues, pull requests and discussions.
- By contributing, you agree that your contribution is released under the project's [PolyForm Noncommercial License 1.0.0](../LICENSE).

### Bugs and ideas

Report a bug with the [issue form](https://github.com/jackson-cmd/zeus-options-terminal/issues/new/choose): what you did, what you expected and what happened. Ask questions and share ideas in [Discussions](https://github.com/jackson-cmd/zeus-options-terminal/discussions). Leave out tokens, cookies and account numbers. Security problems go to a private report, as [SECURITY.md](SECURITY.md) explains.

---

## 中文

ZEUS 的功能远比 README 能写下的多，而且还在长。欢迎提 PR：修 bug、接新的数据源或券商、改翻译、补文档，或者把[还没接上的部分](../docs/FEATURES_zh.md#还没接上的部分欢迎-pr)接起来都可以。

### 怎么提 PR

1. 改动比较大的话，先开一个 Issue，把做法商量好。
2. Fork 仓库，从 `main` 拉一个新分支。
3. 装好依赖，跑测试。测试离线运行，行情是假的。
   ```bash
   python -m pip install -r requirements.txt pytest
   python -m pytest
   ```
4. 装一次隐私检查：`tools/install_git_hook.sh`。它会拦下数据文件、密钥和你自己的路径。你原来的 pre-commit 钩子会保留，检查完照常运行。
5. 向 `main` 提 PR。CI 会在 Linux、macOS、Windows 上跑测试（7 个任务），再打出 pip 安装包、用它启动 `zeus`（1 个任务），8 个全部通过才能合并。`main` 只接受通过 PR 进来的改动。

### 发新版本

合并一个提高了版本号的 PR，就会自动发版。在 PR 里把 `zeus/zeus_config.py` 的 `APP_VERSION` 和 `server.json` 里的三处版本号（服务器、PyPI 包、镜像标签）改成新版本号，并在 `CHANGELOG.md` 和 `CHANGELOG_zh.md` 最上面各加一节写这个版本。合并后，Release 工作流给这个提交打上 `v<版本号>` 标签，发布 GitHub Release，说明就用这两节。版本号和两份更新记录对不上时，`tests/test_release.py` 会失败。同一次运行还会启动 Docker 镜像构建；仓库变量 `PYPI_PUBLISH` 为 `true` 时，`tools/package_smoke.sh` 装好并启动一遍安装包之后，把这个版本上传到 PyPI（`zeus-options-terminal`）。镜像推送完以后，Docker 工作流把这个版本登记到官方 MCP Registry，名字是 `io.github.jackson-cmd/zeus-options-terminal`（`server.json`）；`server.json` 和 `APP_VERSION` 对不上时，`tests/test_mcp_registry.py` 会失败。文档、测试这类用户感觉不到的改动，不用改版本号。

### 东西放在哪

所有代码都在 `zeus/` 里；仓库最上层放文档、网站、测试和启动脚本。

```
zeus-options-terminal/
├── zeus/                 全部代码
│   ├── options_analyzer/   服务器（optionstrat_server.py）、持仓、体检、模拟盘、页面资源
│   ├── heatmap/ · ta_screener/ · ai_picks/ · stock_selector/ · barchart/      选股和扫描
│   ├── benzinga_digest/ · news_radar/ · market_brief/ · sec_filings/ …        新闻、市场判断、事件
│   ├── trade_journal/ · journal_review/ · journal_expiry_whatif/              交易日记
│   ├── zeus_*.py · local_broker.py    设置、券商（Alpaca、Public）、AI、新闻、MCP、邮件
│   ├── Dockerfile          ghcr.io 上的容器镜像（还有 Dockerfile.dockerignore）
│   └── defaults/           第一次启动时放进去的股票清单
├── docs/                 使用手册、功能清单、截图、README 用的图
├── site/                 项目网站（GitHub Pages）
├── tests/                离线测试：python -m pytest
├── tools/                隐私检查钩子、干净打包脚本、发版说明、Docker 镜像和 pip 安装包的检查
├── examples/             手动交易的 CSV 模板
├── .github/              CI 和发版工作流、Issue 表单、CONTRIBUTING、安全政策、行为准则
├── server.json · glama.json · CITATION.cff   MCP Registry、Glama 和 Zenodo 用的登记信息
└── start.sh · start.command · start.bat · requirements.txt · pyproject.toml
```

### 约定

- 不要提交任何凭证、账户号、真实交易，以及 `~/.zeus-terminal/` 里的任何东西。
- 新功能单独放一个模块：接口一个 router，页面一段 JS 或一个静态文件，在服务器里用几行接上。`zeus/options_analyzer/paper_trade_routes.py` 和 `zeus/options_analyzer/paper_trade.py` 是一个小例子。
- 新接口要配测试，行情用假的，参考 `tests/test_paper_trade.py`。
- 面向用户的文字两种语言一起改：`README.md` 和 `README_zh.md`、`docs/*`，新加的中文界面文字要在 `zeus/options_analyzer/static/i18n_en.json` 里补英文。
- 中文句子整句加进 `i18n_en.json`，别拆成词。带数字拼出来的句子当不了词典的键，就在拼句子的地方给英文：页面脚本里写 `zt('剩 ' + n + ' 天', n + ' days left')`（提示框用 `ztt`），服务器发出的数据在中文旁边加一个 `_en` 字段。`tests/test_english_text.py` 两样都会查。
- 友善待人：[行为准则](CODE_OF_CONDUCT.md) 适用于 Issue、PR 和 Discussions。
- 提交贡献即表示你同意它按本项目的 [PolyForm Noncommercial License 1.0.0](../LICENSE) 发布。

### 报 bug、提想法

用 [Issue 表单](https://github.com/jackson-cmd/zeus-options-terminal/issues/new/choose)报 bug：你做了什么、本来想看到什么、实际看到了什么。提问和想法发到 [Discussions](https://github.com/jackson-cmd/zeus-options-terminal/discussions)。别贴 token、cookie 和账户号。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。
