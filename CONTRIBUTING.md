# Contributing to ZEUS Terminal · 参与贡献

[English](#english) · [中文](#中文)

---

## English

ZEUS has far more features than the README can show, and it keeps growing. Pull requests are welcome: bug fixes, new data sources or brokers, translations, docs, and the parts listed under [Not wired up yet](docs/FEATURES.md#not-wired-up-yet--ideas-for-pull-requests).

### Sending a pull request

1. For anything bigger than a small fix, open an issue first so we can agree on the approach.
2. Fork the repository and create a branch from `main`.
3. Set up and run the tests. They run offline; quotes are faked.
   ```bash
   python -m pip install -r requirements.txt pytest
   python -m pytest
   ```
4. Install the privacy hook once: `tools/install_git_hook.sh`. It refuses data files, keys and your own paths in a commit.
5. Open a pull request against `main`. CI runs the tests on Linux, macOS and Windows (7 jobs), and a pull request can merge only when all of them pass. `main` only accepts changes through pull requests.

### Guidelines

- Never commit credentials, account numbers, real trades or anything from `~/.zeus-terminal/`.
- Keep a new feature in its own module: a router for the API and a JS string or static file for the page, wired into the server in a few lines. `zeus/options_analyzer/paper_trade_routes.py` and `options_analyzer/paper_trade.py` are a small example.
- Add tests for new endpoints, with faked market data as in `tests/test_paper_trade.py`.
- Update user-facing text in both languages: `README.md` and `README_zh.md`, `docs/*`, and `zeus/options_analyzer/static/i18n_en.json` for new Chinese interface strings.
- Add each Chinese sentence to `i18n_en.json` whole, never piece by piece. A sentence built with numbers cannot be a dictionary key, so give its English where it is built: `zt('剩 ' + n + ' 天', n + ' days left')` in a page script (`ztt` for a tooltip), or an `_en` field next to the Chinese in data the server sends. `tests/test_english_text.py` checks both.
- By contributing, you agree that your contribution is released under the project's [PolyForm Noncommercial License 1.0.0](LICENSE).

### Bugs and ideas

Open an issue with what you did, what you expected and what happened. Leave out tokens, cookies and account numbers.

---

## 中文

ZEUS 的功能远比 README 能写下的多，而且还在长。欢迎提 PR：修 bug、接新的数据源或券商、改翻译、补文档，或者把[还没接上的部分](docs/FEATURES_zh.md#还没接上的部分欢迎-pr)接起来都可以。

### 怎么提 PR

1. 改动比较大的话，先开一个 Issue，把做法商量好。
2. Fork 仓库，从 `main` 拉一个新分支。
3. 装好依赖，跑测试。测试离线运行，行情是假的。
   ```bash
   python -m pip install -r requirements.txt pytest
   python -m pytest
   ```
4. 装一次隐私检查：`tools/install_git_hook.sh`。它会拦下数据文件、密钥和你自己的路径。
5. 向 `main` 提 PR。CI 会在 Linux、macOS、Windows 上跑测试（7 个任务），全部通过才能合并。`main` 只接受通过 PR 进来的改动。

### 约定

- 不要提交任何凭证、账户号、真实交易，以及 `~/.zeus-terminal/` 里的任何东西。
- 新功能单独放一个模块：接口一个 router，页面一段 JS 或一个静态文件，在服务器里用几行接上。`zeus/options_analyzer/paper_trade_routes.py` 和 `options_analyzer/paper_trade.py` 是一个小例子。
- 新接口要配测试，行情用假的，参考 `tests/test_paper_trade.py`。
- 面向用户的文字两种语言一起改：`README.md` 和 `README_zh.md`、`docs/*`，新加的中文界面文字要在 `zeus/options_analyzer/static/i18n_en.json` 里补英文。
- 中文句子整句加进 `i18n_en.json`，别拆成词。带数字拼出来的句子当不了词典的键，就在拼句子的地方给英文：页面脚本里写 `zt('剩 ' + n + ' 天', n + ' days left')`（提示框用 `ztt`），服务器发出的数据在中文旁边加一个 `_en` 字段。`tests/test_english_text.py` 两样都会查。
- 提交贡献即表示你同意它按本项目的 [PolyForm Noncommercial License 1.0.0](LICENSE) 发布。

### 报 bug、提想法

开一个 Issue，写清楚你做了什么、本来想看到什么、实际看到了什么。别贴 token、cookie 和账户号。
