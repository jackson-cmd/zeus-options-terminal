"""ZEUS Terminal (宙斯期权终端) settings store.

Every credential and preference lives in ONE local JSON file:

    ~/.zeus-terminal/config.json        (override the folder with ZEUS_HOME)

The file is created with 0600 permissions and is never inside the repository,
so a `git push` can never carry a token. The web UI writes it through
/api/settings; secret values are write-only from the browser's point of view
(the API reports "set / not set" and never sends a secret back).

Any module can do:

    import zeus_config as hc
    hc.get("finviz_elite_token")
    hc.account_source()     # 'tastytrade' | 'alpaca' | 'public' | 'manual'
    hc.market_source()      # 'tastytrade' | 'alpaca' | 'public' | 'free'
"""
from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from typing import Any

APP_NAME = "ZEUS Terminal"
APP_NAME_ZH = "宙斯期权终端"
APP_VERSION = "0.5.0"
APP_TAGLINE = "Self-hosted, all-in-one options trading terminal"
APP_TAGLINE_ZH = "本地部署的全方位期权交易终端"
REPO_ROOT = Path(__file__).resolve().parent

_lock = threading.Lock()
_cache: dict | None = None
_cache_mtime: float = -1.0


def home() -> Path:
    p = Path(os.environ.get("ZEUS_HOME") or (Path.home() / ".zeus-terminal")).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, stat.S_IRWXU)
    except OSError:
        pass
    return p


def config_path() -> Path:
    return home() / "config.json"


# ---------------------------------------------------------------------------
# Schema: key -> (default, is_secret, group, label_en, label_zh, help_en, help_zh)
# The settings page is rendered from this table, so adding a key here is all it
# takes to expose a new option in the UI.
# ---------------------------------------------------------------------------
SCHEMA: dict[str, tuple] = {
    # ---- general ----
    "language": ("auto", False, "general", "Interface language", "界面语言",
                 "auto = follow the browser; zh = Chinese; bi = bilingual (Chinese + English side by side); en = English",
                 "auto = 跟随浏览器；zh = 中文；bi = 双语（中英并排）；en = English"),
    "host": ("127.0.0.1", False, "general", "Listen address", "监听地址",
             "Keep 127.0.0.1 unless you know why. 0.0.0.0 exposes your portfolio to the whole network.",
             "保持 127.0.0.1。改成 0.0.0.0 会把持仓暴露给整个局域网。"),
    "port": ("8765", False, "general", "Port", "端口", "Restart to apply.", "重启后生效。"),
    "desktop_notifications": ("on", False, "general", "Desktop notifications (macOS)", "桌面通知（macOS）",
                              "on / off. Take-profit, missing GTC, risk radar, S/R alerts.",
                              "on / off。止盈、未挂 GTC、风险雷达、支撑阻力告警。"),
    # ---- broker ----
    "account_source": ("auto", False, "broker", "Positions come from", "持仓来源",
                       "auto = the first broker filled in below (tastytrade, then Alpaca, then Public), otherwise your "
                       "uploaded trades. Or pick one: tastytrade / alpaca / public / manual.",
                       "auto = 按 tastytrade、Alpaca、Public 的顺序用第一个填好的券商，都没填就用你上传的交易。"
                       "也可以直接指定：tastytrade / alpaca / public / manual。"),
    "market_data": ("auto", False, "broker", "Quotes come from", "行情来源",
                    "auto = your broker's own data (tastytrade, Alpaca or Public), otherwise free. free = Cboe options "
                    "(about 15 min delayed, with Greeks) + Yahoo stocks, no account needed. alpaca / public = that API; "
                    "anything it lacks, such as the SPX and VIX index levels, still comes from the free sources. "
                    "With tastytrade as the broker, quotes always stream from tastytrade.",
                    "auto = 用券商自己的行情（tastytrade、Alpaca 或 Public），没有就用免费的。free = Cboe 期权（约延迟 "
                    "15 分钟，带希腊值）+ Yahoo 股票，不用开户。alpaca / public = 用该券商的接口；它没有的数据（比如 SPX、"
                    "VIX 指数点位）照样走免费源。券商选 tastytrade 时，行情一律走 tastytrade 实时推送。"),
    "tt_client_id": ("", True, "broker", "tastytrade OAuth client ID", "tastytrade OAuth Client ID",
                     "my.tastytrade.com → Manage → API Access → OAuth Applications.",
                     "my.tastytrade.com → Manage → API Access → OAuth Applications。"),
    "tt_client_secret": ("", True, "broker", "tastytrade OAuth client secret", "tastytrade OAuth Client Secret",
                         "Shown once when you create the OAuth application.",
                         "创建 OAuth 应用时只显示一次。"),
    "tt_refresh_token": ("", True, "broker", "tastytrade refresh token", "tastytrade Refresh Token",
                         "OAuth application → Manage → Create Grant (select all scopes).",
                         "OAuth 应用 → Manage → Create Grant（勾选全部 scope）。"),
    "tt_account_number": ("", True, "broker", "tastytrade account number (optional)", "tastytrade 账户号（可选）",
                          "Only needed if you hold several accounts.", "有多个账户时才需要填。"),
    "alpaca_key_id": ("", True, "broker", "Alpaca API key ID", "Alpaca API Key ID",
                      "alpaca.markets → sign up (free) → Paper Trading → API Keys → Generate. Paper keys start with PK, "
                      "live keys with AK.",
                      "alpaca.markets 免费注册 → Paper Trading → API Keys → Generate。模拟盘的 key 以 PK 开头，实盘以 AK 开头。"),
    "alpaca_secret_key": ("", True, "broker", "Alpaca secret key", "Alpaca Secret Key",
                          "Shown once, next to the key ID.", "和 Key ID 一起生成，只显示一次。"),
    "alpaca_paper": ("on", False, "broker", "Alpaca paper account", "Alpaca 模拟盘",
                     "on = your paper account (paper-api.alpaca.markets); off = your live account.",
                     "on = 模拟盘账户（paper-api.alpaca.markets）；off = 实盘账户。"),
    "alpaca_stock_feed": ("iex", False, "broker", "Alpaca stock feed", "Alpaca 股票行情",
                          "iex = free, real time, prices from the IEX exchange only; delayed_sip = free, every exchange, "
                          "15 min delayed; sip = every exchange in real time (Alpaca's paid data plan).",
                          "iex = 免费、实时，只含 IEX 交易所的成交；delayed_sip = 免费、全市场、延迟 15 分钟；"
                          "sip = 全市场实时（Alpaca 付费行情）。"),
    "alpaca_options_feed": ("indicative", False, "broker", "Alpaca options feed", "Alpaca 期权行情",
                            "indicative = free: quotes are Alpaca's indicative prices, trades 15 min delayed, Greeks "
                            "and IV included; opra = the real-time exchange feed (Alpaca's paid data plan).",
                            "indicative = 免费：报价是 Alpaca 的参考价，成交延迟 15 分钟，带希腊值和 IV；"
                            "opra = 交易所实时（Alpaca 付费行情）。"),
    "public_secret_key": ("", True, "broker", "Public API secret key", "Public API Secret Key",
                          "public.com → Account → Settings → API → Generate secret key. Public has live accounts only "
                          "(no paper trading); ZEUS only reads, it never places orders.",
                          "public.com → 账户 → Settings → API → 生成 secret key。Public 只有实盘（没有模拟盘）；"
                          "ZEUS 只读取，从不下单。"),
    "public_account_id": ("", True, "broker", "Public account ID (optional)", "Public 账户 ID（可选）",
                          "Only needed if you hold several Public accounts; empty = your brokerage account.",
                          "有多个 Public 账户时才需要填；留空 = 你的证券账户。"),
    "manual_starting_cash": ("0", False, "broker", "Manual mode: starting cash", "手动模式：初始现金",
                             "Cash before your first uploaded trade. Drives NLV and position sizing.",
                             "第一笔上传交易之前的现金，用来算净值和仓位占比。"),
    # ---- news ----
    "benzinga_cookies_json": ("", True, "news", "Benzinga Pro cookies (JSON)", "Benzinga Pro 登录 cookie（JSON）",
                              "Export pro.benzinga.com cookies (e.g. with a cookie-export extension) and paste the JSON array.",
                              "用 cookie 导出扩展导出 pro.benzinga.com 的 cookie，把 JSON 数组整段粘进来。"),
    "benzinga_data_apikey": ("", True, "news", "Benzinga data API key (optional)", "Benzinga data API key（可选）",
                             "Enables the real-time signals feed and the scanner.",
                             "开启实时信号和扫描器。"),
    "benzinga_username": ("", True, "news", "Benzinga login e-mail (optional)", "Benzinga 登录邮箱（可选）",
                          "Only for the optional auto re-login helper (premarket/login.mjs).",
                          "只给可选的自动续登脚本 premarket/login.mjs 用。"),
    "benzinga_password": ("", True, "news", "Benzinga password (optional)", "Benzinga 密码（可选）",
                          "Stored only in ~/.zeus-terminal/config.json on this computer.",
                          "只存在本机 ~/.zeus-terminal/config.json。"),
    "finviz_elite_token": ("", True, "news", "Finviz Elite auth token (optional)", "Finviz Elite auth token（可选）",
                           "Without it the free finviz.com news page is used.",
                           "不填就用免费的 finviz.com 新闻页。"),
    "free_news_sources": ("finviz,wsj,marketwatch", False, "news", "Free news sources", "免费新闻源",
                          "Comma list of: finviz, wsj, marketwatch.",
                          "逗号分隔：finviz、wsj、marketwatch。"),
    "news_translate": ("google", False, "news", "Headline translation", "标题翻译",
                       "google / bing / ai / off. Makes every headline bilingual (google rotates Google, Bing and local engines).",
                       "google / bing / ai / off，把每条标题做成中英双语（google 会在 Google、Bing 和本地引擎之间轮换）。"),
    "google_translate_key": ("", True, "news", "Google translate key (optional)", "Google 翻译 key（可选）",
                             "Without it headlines are translated with Bing (free) or your AI provider.",
                             "不填也能翻：会用 Bing（免费）或你配置的 AI。"),
    # ---- AI ----
    "ai_provider": ("auto", False, "ai", "AI provider", "AI 提供方",
                    "auto / claude_cli / anthropic / openai / off. auto picks the first one that is configured.",
                    "auto / claude_cli / anthropic / openai / off。auto 会用第一个配好的。"),
    "claude_bin": ("", False, "ai", "Claude Code CLI path (optional)", "Claude Code CLI 路径（可选）",
                   "Uses your Claude subscription through `claude -p`. Leave empty to search PATH.",
                   "通过 `claude -p` 用你自己的 Claude 订阅；留空则在 PATH 里找。"),
    "anthropic_api_key": ("", True, "ai", "Anthropic API key", "Anthropic API key", "", ""),
    "openai_api_key": ("", True, "ai", "OpenAI-compatible API key", "OpenAI 兼容 API key",
                       "Works with OpenAI, DeepSeek, OpenRouter, a local Ollama, …",
                       "OpenAI、DeepSeek、OpenRouter、本地 Ollama 都行。"),
    "openai_base_url": ("https://api.openai.com/v1", False, "ai", "OpenAI-compatible base URL", "OpenAI 兼容 Base URL",
                        "e.g. https://api.deepseek.com/v1 or http://localhost:11434/v1",
                        "例如 https://api.deepseek.com/v1 或 http://localhost:11434/v1"),
    "ai_model_fast": ("", False, "ai", "Fast model", "快速模型",
                      "Cheap model for summaries. Empty = provider default (Claude Haiku; for OpenAI-compatible endpoints fill in a model name).",
                      "做总结用的便宜模型；留空用默认（Claude Haiku；OpenAI 兼容接口请填模型名）。"),
    "ai_model_smart": ("", False, "ai", "Smart model", "强模型",
                       "Model for stock picking and reviews. Empty = provider default.",
                       "选股和复盘用的模型；留空用默认。"),
    "ai_language": ("auto", False, "ai", "AI answer language", "AI 回答语言",
                    "auto (follow interface) / zh / en", "auto（跟随界面）/ zh / en"),
    "ai_schedules": ("on", False, "ai", "Scheduled AI jobs", "定时 AI 任务",
                     "on / off. Morning digest summary, market brief, AI picks, position review.",
                     "on / off：早盘新闻总结、市场判断、AI 选股、持仓点评。"),
    # ---- AI agents (MCP) ----
    "mcp_server": ("on", False, "agents", "MCP server for AI agents", "给 AI 智能体的 MCP 接口",
                   "on / off. Lets agents on this computer (Claude, GPT through Codex, Gemini, your own) read ZEUS "
                   "at /mcp. Read-only: no agent can place a real order or see a saved key.",
                   "on / off。让本机的智能体（Claude、通过 Codex 的 GPT、Gemini、你自己写的）通过 /mcp 读取 ZEUS。"
                   "只读：任何智能体都下不了真实订单，也看不到已保存的密钥。"),
    "mcp_paper_trades": ("off", False, "agents", "Agents may place paper trades", "允许智能体下模拟单",
                         "on / off. Lets agents open and close trades in the Paper Trade tab. Simulated: nothing "
                         "is ever sent to a broker.",
                         "on / off。允许智能体在模拟盘里开仓、平仓。全是模拟，不会发给任何券商。"),
    # ---- other data ----
    "barchart_username": ("", True, "data", "Barchart username (optional)", "Barchart 账号（可选）",
                          "Enables the Barchart screener / signal lists.", "开启 Barchart 筛选和信号榜单。"),
    "barchart_password": ("", True, "data", "Barchart password (optional)", "Barchart 密码（可选）", "", ""),
    "finviz_ai_crawl": ("on", False, "data", "Finviz 'why is it moving' crawl", "Finviz「为何异动」抓取",
                        "on / off. Slow polite crawl (~1 req/s) of your universe.",
                        "on / off：按票池慢速抓取（约每秒 1 次）。"),
    "sec_user_agent": ("ZEUS research contact@example.com", False, "data", "SEC EDGAR User-Agent",
                       "SEC EDGAR User-Agent",
                       "SEC asks for a contact e-mail in the User-Agent. Put your own.",
                       "SEC 要求 User-Agent 里带联系邮箱，填你自己的。"),
    # ---- e-mail alerts ----
    "email_from": ("", True, "email", "Alert e-mail: from", "邮件提醒：发件人",
                   "The account that sends the alerts; it also logs in to the SMTP server.",
                   "用来发提醒的邮箱，也用它登录 SMTP 服务器。"),
    "email_to": ("", True, "email", "Alert e-mail: to", "邮件提醒：收件人",
                 "Where alerts go. Several addresses: separate them with commas.",
                 "提醒发到哪里。多个地址用逗号隔开。"),
    "email_app_password": ("", True, "email", "SMTP app password", "SMTP 应用密码",
                           "Gmail: create an app password under Google Account → Security. Not your normal password.",
                           "Gmail：在 Google 账号 → 安全性 里生成应用专用密码，不是平时登录的密码。"),
    "email_smtp_server": ("smtp.gmail.com", False, "email", "SMTP server", "SMTP 服务器", "", ""),
    "email_smtp_port": ("587", False, "email", "SMTP port", "SMTP 端口",
                        "587 (STARTTLS) or 465 (SSL).", "587（STARTTLS）或 465（SSL）。"),
}

GROUPS = [
    ("broker", "Broker & data", "券商与行情"),
    ("news", "News sources", "新闻源"),
    ("ai", "AI", "AI"),
    ("agents", "AI agents (MCP)", "AI 智能体（MCP）"),
    ("data", "Other data", "其他数据"),
    ("email", "E-mail alerts", "邮件提醒"),
    ("general", "General", "通用"),
]

# Environment variables that override the file (handy for Docker / servers).
_ENV = {k: "ZEUS_" + k.upper() for k in SCHEMA}


def _load() -> dict:
    global _cache, _cache_mtime
    p = config_path()
    try:
        mt = p.stat().st_mtime
    except FileNotFoundError:
        _cache, _cache_mtime = {}, -1.0
        return _cache
    if _cache is not None and mt == _cache_mtime:
        return _cache
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    _cache, _cache_mtime = data, mt
    return data


def get(key: str, default: Any = None) -> Any:
    env = os.environ.get(_ENV.get(key, ""), "") if key in _ENV else ""
    if env:
        return env
    with _lock:
        val = _load().get(key)
    if val not in (None, ""):
        return val
    if default is not None:
        return default
    return SCHEMA[key][0] if key in SCHEMA else None


def get_bool(key: str) -> bool:
    return str(get(key) or "").strip().lower() in ("1", "on", "true", "yes", "y")


def update(values: dict) -> dict:
    """Merge `values` into the config file. Empty string clears a key.
    Unknown keys are ignored. Returns the public view."""
    with _lock:
        data = dict(_load())
        for k, v in (values or {}).items():
            if k not in SCHEMA:
                continue
            v = "" if v is None else str(v).strip()
            if v == "":
                data.pop(k, None)
            else:
                data[k] = v
        p = config_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        os.replace(tmp, p)
        global _cache, _cache_mtime
        _cache, _cache_mtime = None, -1.0
    return public_view()


def public_view() -> dict:
    """What the browser may see: non-secret values, and only set/not-set for secrets."""
    out = {}
    for k, (default, secret, group, en, zh, hen, hzh) in SCHEMA.items():
        v = get(k)
        item = {"group": group, "label_en": en, "label_zh": zh, "help_en": hen, "help_zh": hzh,
                "secret": secret}
        if secret:
            item["is_set"] = bool(v)
        else:
            item["value"] = v
        out[k] = item
    return {"settings": out, "groups": GROUPS, "status": status()}


# ---------------------------------------------------------------------------
# Capability checks used to switch features on/off
# ---------------------------------------------------------------------------
def has_tastytrade() -> bool:
    return bool(get("tt_client_secret") and get("tt_refresh_token"))


def has_alpaca() -> bool:
    return bool(get("alpaca_key_id") and get("alpaca_secret_key"))


def has_public() -> bool:
    return bool(get("public_secret_key"))


BROKERS = ("tastytrade", "alpaca", "public")


def _has(src: str) -> bool:
    return {"tastytrade": has_tastytrade, "alpaca": has_alpaca, "public": has_public}.get(src, lambda: False)()


def account_source() -> str:
    """Where positions, balances and fills come from: 'tastytrade', 'alpaca', 'public' or 'manual'.
    A broker that was picked but has no keys yet falls back to 'manual' (status() says so)."""
    want = str(get("account_source") or "auto").strip().lower()
    if want == "manual":
        return "manual"
    if want in BROKERS:
        return want if _has(want) else "manual"
    return next((b for b in BROKERS if _has(b)), "manual")


def market_source() -> str:
    """Where quotes, option chains and Greeks come from: 'tastytrade', 'alpaca', 'public' or 'free'.
    With tastytrade as the broker everything streams from tastytrade."""
    acct = account_source()
    if acct == "tastytrade":
        return "tastytrade"
    want = str(get("market_data") or "auto").strip().lower()
    if want == "free":
        return "free"
    if want in ("alpaca", "public"):
        return want if _has(want) else "free"
    if acct in ("alpaca", "public"):
        return acct
    return next((b for b in ("alpaca", "public") if _has(b)), "free")


def broker_mode() -> str:
    """Kept for older callers: the account source ('tastytrade', 'alpaca', 'public' or 'manual')."""
    return account_source()


def has_benzinga() -> bool:
    return bool(get("benzinga_cookies_json"))


def has_benzinga_apikey() -> bool:
    return bool(get("benzinga_data_apikey"))


def has_barchart() -> bool:
    return bool(get("barchart_username") and get("barchart_password"))


def notifications_enabled() -> bool:
    import sys
    return sys.platform == "darwin" and get_bool("desktop_notifications")


def email_configured() -> bool:
    """Sender, a recipient, app password and SMTP server are all filled in."""
    return bool(get("email_from") and "@" in str(get("email_to") or "")
                and get("email_app_password") and get("email_smtp_server"))


def ai_schedules_enabled() -> bool:
    try:
        import zeus_ai
        return get_bool("ai_schedules") and zeus_ai.available()
    except Exception:
        return False


def status() -> dict:
    try:
        import zeus_ai
        ai = zeus_ai.describe()
    except Exception as e:  # pragma: no cover
        ai = {"provider": "off", "error": str(e)}
    want = str(get("account_source") or "auto").strip().lower()
    want_md = str(get("market_data") or "auto").strip().lower()
    return {
        "broker_mode": broker_mode(),
        "market_source": market_source(),
        "brokers": {"tastytrade": has_tastytrade(), "alpaca": has_alpaca(), "public": has_public()},
        "alpaca_paper": get_bool("alpaca_paper"),
        # a broker was picked but its keys are missing, so ZEUS fell back
        "broker_fallback": want if (want in BROKERS and not _has(want)) else "",
        "market_fallback": want_md if (want_md in ("alpaca", "public") and not _has(want_md)) else "",
        "benzinga": has_benzinga(),
        "benzinga_apikey": has_benzinga_apikey(),
        "finviz_elite": bool(get("finviz_elite_token")),
        "barchart": has_barchart(),
        "ai": ai,
        "mcp": get_bool("mcp_server"),
        "mcp_paper_trades": get_bool("mcp_paper_trades"),
        "config_file": str(config_path()),
    }


# ---------------------------------------------------------------------------
# Benzinga cookie file bridge: the Benzinga clients read a cookies.json file.
# We materialise it from the pasted JSON into ~/.zeus-terminal (never the repo).
# ---------------------------------------------------------------------------
def benzinga_cookies_path() -> Path:
    p = home() / "benzinga_cookies.json"
    raw = get("benzinga_cookies_json")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "cookies" in data:
                data = data["cookies"]
            if isinstance(data, list):
                cur = None
                try:
                    cur = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    pass
                if cur != data:
                    p.write_text(json.dumps(data), encoding="utf-8")
                    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass
    return p


def data_dir(*parts: str) -> Path:
    """Repository-local runtime data folder (git-ignored)."""
    p = REPO_ROOT.joinpath("data", *parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


def barchart_credentials_file() -> Path:
    """The Barchart scrapers read a two-column CSV (username,password). We write
    it from Settings into ~/.zeus-terminal (0600); it never lives in the repository."""
    p = home() / "barchart_credentials.csv"
    u, pw = str(get("barchart_username") or ""), str(get("barchart_password") or "")
    if u and pw:
        import csv as _csv
        import io as _io
        buf = _io.StringIO()
        w = _csv.writer(buf)
        w.writerow(["username", "password"])
        w.writerow([u, pw])
        if not p.exists() or p.read_text(encoding="utf-8") != buf.getvalue():
            p.write_text(buf.getvalue(), encoding="utf-8")
            os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    elif p.exists():
        p.unlink()
    return p


def apply_notification_shims() -> None:
    """Desktop alerts go out through `osascript` / `terminal-notifier`, so stand-ins
    with those names first on PATH steer every alert in every module:

    - e-mail set up: relay stand-ins mail each alert (zeus_mail) and pass it on
      to the real notifier when desktop notifications are on;
    - otherwise, desktop notifications off (or not macOS): no-op stand-ins;
    - otherwise: the real programs."""
    import shlex
    import sys
    noop = home() / "bin" / "quiet"
    relay = home() / "bin" / "relay"
    parts = [x for x in os.environ.get("PATH", "").split(os.pathsep) if x and x not in (str(noop), str(relay))]
    if email_configured() and os.name != "nt":
        relay.mkdir(parents=True, exist_ok=True)
        for name in ("osascript", "terminal-notifier"):
            f = relay / name
            f.write_text("#!/bin/sh\n"
                         f"PYTHONPATH={shlex.quote(str(REPO_ROOT))}${{PYTHONPATH:+:$PYTHONPATH}} "
                         f"exec {shlex.quote(sys.executable)} -m zeus_mail shim {name} \"$@\"\n", encoding="utf-8")
            f.chmod(0o755)
        os.environ["PATH"] = os.pathsep.join([str(relay)] + parts)
        return
    if notifications_enabled():
        os.environ["PATH"] = os.pathsep.join(parts)
        return
    noop.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        for name in ("osascript", "terminal-notifier"):
            f = noop / name
            if not f.exists():
                f.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                f.chmod(0o755)
    os.environ["PATH"] = os.pathsep.join([str(noop)] + parts)
    _ = sys  # keep import for linters


def ensure_default_data() -> None:
    """First run: seed the ticker universe files every screen reads, including
    the industry groups behind sector rotation and options sentiment. Your own
    lists replace these any time (edit data/heatmap_universe.txt or
    data/industry_universe.txt); existing files are never overwritten."""
    src = REPO_ROOT / "defaults" / "heatmap_universe.txt"
    if not src.exists():
        return
    raw = src.read_text(encoding="utf-8")
    tickers, seen = [], set()
    for tok in raw.replace("\n", ",").split(","):
        tok = tok.strip()
        if not tok or tok.startswith("###"):
            continue
        sym = tok.split(":")[-1].strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            tickers.append(sym)
    stocks = [t for t in tickers]
    etf_src = REPO_ROOT / "defaults" / "etf_trends.txt"
    ind_src = REPO_ROOT / "defaults" / "industry_universe.txt"
    targets = {
        data_dir() / "heatmap_universe.txt": raw,
        data_dir() / "etf_trends.txt": etf_src.read_text(encoding="utf-8") if etf_src.exists() else "",
        data_dir() / "industry_universe.txt": ind_src.read_text(encoding="utf-8") if ind_src.exists() else "",
        data_dir() / "tickers.csv": "ticker\n" + "\n".join(stocks) + "\n",
        data_dir("options_vol") / "core_universe.txt": "\n".join(stocks) + "\n",
        data_dir() / "oa_watchlist.txt": "\n".join(stocks) + "\n",
        data_dir() / "watchlist.txt": "\n".join(stocks) + "\n",
    }
    for path, content in targets.items():
        if content and not path.exists():      # an empty file would stop a later seed
            path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Runtime data lives OUTSIDE the code folder.
# Every directory the app writes at runtime (position snapshots, trade journal,
# screenshots, news archives, caches, downloads, logs) is turned into a link to
# ~/.zeus-terminal/runtime/…, so the code folder only ever contains code: neither
# `git add .` nor a drag-and-drop upload of the folder can carry your data.
# ---------------------------------------------------------------------------
RUNTIME_DIRS = (
    "data", "options_analyzer/data", "trade_journal/data", "pre_trade_checkup/data", "stock_picker/data",
    "eod_crawler/data_eod", "eod_crawler/logs", "barchart/downloads", "barchart/snapshots",
    "benzinga_digest/digests", "benzinga_digest/ai_summary", "benzinga_digest/pivot_summary",
    "benzinga_digest/finviz_ai",
    "trading_assistant/data", "trading_assistant/reports", "winrate_scanner/data",
)


def runtime_root() -> Path:
    p = home() / "runtime"
    p.mkdir(parents=True, exist_ok=True)
    return p


def relocate_runtime_dirs() -> list[str]:
    """Make each runtime directory a symlink into ~/.zeus-terminal/runtime.
    Existing contents (from older versions) are moved over first. On systems
    where symlinks are not allowed (some Windows setups) the directory simply
    stays in place and is still ignored by git. Returns the dirs left in place."""
    import shutil
    left = []
    base = runtime_root()
    for rel in RUNTIME_DIRS:
        link = REPO_ROOT / rel
        target = base / rel.replace("/", "__")
        target.mkdir(parents=True, exist_ok=True)
        try:
            if link.is_symlink():
                if Path(os.readlink(link)) != target:
                    link.unlink()
                    link.symlink_to(target, target_is_directory=True)
                continue
            if link.is_dir():
                for child in list(link.iterdir()):
                    dest = target / child.name
                    if not dest.exists():
                        shutil.move(str(child), str(dest))
                if any(link.iterdir()):
                    left.append(rel)        # name clash: keep the folder, never delete data
                    continue
                link.rmdir()
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            link.mkdir(parents=True, exist_ok=True)
            left.append(rel)
    return left
