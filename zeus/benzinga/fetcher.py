"""Benzinga Options School chat fetcher.

Delegates scraping to premarket/fetch.mjs (puppeteer-extra stealth + real Chrome
profile fingerprint). Stores text (no images/PDF binaries) in
data/benzinga/YYYY-MM-DD.json.

Channels:
  - chris-market-commentary (daily)
  - options-flow-report     (weekly, Mondays)
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import zeus_config as _hc  # noqa: E402
# Benzinga cookies are pasted in Settings and written to ~/.zeus-terminal (never the repo)
COOKIES_FILE = _hc.benzinga_cookies_path()
DATA_DIR = REPO_ROOT / "data" / "benzinga"
DATA_DIR.mkdir(parents=True, exist_ok=True)

CHAT_URL = "https://www.benzinga.com/research/options-school/chat"

CHANNELS = {
    "chris-market-commentary": {
        "label": "Chris Market Commentary",
        "selector_text": "Chris Market Commentary",
    },
    "options-flow-report": {
        "label": "Options Flow Report",
        "selector_text": "Options Flow Report",
    },
}




_AUTH_COOKIE_NAMES = ("benzinga_token", "bz_refresh")


def cookie_expiry_status(cookies_file=None) -> dict:
    """登录 cookie 还有多久过期。**只读到期时间戳, 绝不读取/返回 cookie 值。**

    benzinga_token / bz_refresh 是绝对过期(服务端不随访问滚动), 取两者中最早的。
    返回 {expires_at: epoch|None, days_left: float|None, expired: bool, found: bool}。
    """
    path = cookies_file or COOKIES_FILE
    out = {"expires_at": None, "days_left": None, "expired": False, "found": False}
    try:
        raw = json.load(open(path, "r", encoding="utf-8"))
    except Exception:
        return out
    exps = []
    for c in raw:
        if c.get("name") in _AUTH_COOKIE_NAMES:
            e = c.get("expires") or c.get("expirationDate") or 0
            if isinstance(e, (int, float)) and e > 0:
                exps.append(float(e))
    if not exps:
        return out
    soonest = min(exps)
    now = datetime.now(timezone.utc).timestamp()
    out.update(found=True, expires_at=soonest,
               days_left=(soonest - now) / 86400.0, expired=soonest <= now)
    return out


def _auto_relogin() -> bool:
    """调用 premarket/login.mjs (puppeteer stealth) 自动重新登录, 拿新 cookie。
    成功返回 True。"""
    import subprocess
    login_script = Path(__file__).resolve().parent.parent / "premarket" / "login.mjs"
    if not login_script.exists():
        print(f"[err] 找不到 {login_script}", file=sys.stderr)
        return False
    cookie_dst = str(COOKIES_FILE)
    print(f"[info] cookie 过期, 自动调用 puppeteer stealth 重新登录...", file=sys.stderr)
    try:
        r = subprocess.run(
            ["arch", "-arm64", "node", str(login_script), cookie_dst],
            timeout=120, capture_output=True, text=True,
        )
        print(r.stdout, end="", file=sys.stderr)
        if r.stderr:
            print(r.stderr, end="", file=sys.stderr)
        if r.returncode == 0:
            print(f"[info] 自动登录成功, cookie 已更新", file=sys.stderr)
            return True
        print(f"[err] 自动登录失败 (exit {r.returncode})", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print(f"[err] 自动登录超时 (120s)", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[err] 自动登录异常: {e}", file=sys.stderr)
        return False


def _ensure_cookies_valid():
    """检查 cookie 有效性, 过期则自动重新登录。"""
    st = cookie_expiry_status()
    if not st["found"]:
        print("[warn] 没有找到 auth cookie, 尝试自动登录", file=sys.stderr)
        _auto_relogin()
        return
    when = datetime.fromtimestamp(st["expires_at"]).strftime("%Y-%m-%d %H:%M")
    if st["expired"]:
        print(f"[info] cookie 已于 {when} 过期, 自动重新登录", file=sys.stderr)
        _auto_relogin()
    elif st["days_left"] is not None and st["days_left"] <= 3.0:
        print(f"[info] cookie 还有 {st['days_left']:.1f} 天到期({when}), 提前续期", file=sys.stderr)
        _auto_relogin()


def warn_if_expiring(days_warn: float = 7.0) -> dict:
    """临期/已过期就在 stderr 喊一声, 让日志里能提前看见。返回状态供调用方复用。"""
    st = cookie_expiry_status()
    if not st["found"]:
        return st
    when = datetime.fromtimestamp(st["expires_at"]).strftime("%Y-%m-%d %H:%M")
    if st["expired"]:
        print(f"[info] benzinga 登录 cookie 已于 {when} 过期, 将自动续期", file=sys.stderr)
    elif st["days_left"] is not None and st["days_left"] <= days_warn:
        print(f"[warn] benzinga 登录 cookie 还有 {st['days_left']:.1f} 天到期({when})",
              file=sys.stderr)
    return st



def fetch_channels(channel_keys=None, headless=True, debug=False, max_scroll_rounds=8):
    """调用 premarket/fetch.mjs (puppeteer-extra stealth + 真实 Chrome) 抓取 chat。
    Playwright headless Chromium 已被 Cloudflare block, 全部迁移到 Node。"""
    import subprocess

    channel_keys = channel_keys or list(CHANNELS.keys())

    fetch_script = Path(__file__).resolve().parent.parent / "premarket" / "fetch.mjs"
    if not fetch_script.exists():
        raise FileNotFoundError(f"fetch.mjs not found: {fetch_script}")

    cmd = ["arch", "-arm64", "node", str(fetch_script), f"--max-scroll={max_scroll_rounds}"]
    print(f"[info] running: {' '.join(cmd)}", file=sys.stderr)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=600,
            cwd=str(fetch_script.parent.parent),
        )
    except subprocess.TimeoutExpired:
        print("[err] fetch.mjs 超时 (600s)", file=sys.stderr)
        return {"fetched_at": datetime.now(timezone.utc).isoformat(),
                "channels": {k: {"label": CHANNELS[k]["label"], "error": "timeout"}
                             for k in channel_keys}}

    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)

    if proc.returncode != 0:
        print(f"[err] fetch.mjs exit {proc.returncode}", file=sys.stderr)
        return {"fetched_at": datetime.now(timezone.utc).isoformat(),
                "channels": {k: {"label": CHANNELS[k]["label"],
                                 "error": f"fetch.mjs exit {proc.returncode}"}
                             for k in channel_keys}}

    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        print(f"[err] fetch.mjs output 不是有效 JSON: {e}", file=sys.stderr)
        return {"fetched_at": datetime.now(timezone.utc).isoformat(),
                "channels": {k: {"label": CHANNELS[k]["label"], "error": "bad JSON"}
                             for k in channel_keys}}

    return result


def _extract_pdf_text(url: str, timeout: int = 30) -> str:
    """Download PDF (in memory) and return extracted text. Binary is not saved."""
    try:
        from pypdf import PdfReader
    except Exception as e:
        return f"[pdf extract failed: pypdf import: {e}]"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        reader = PdfReader(io.BytesIO(data))
        pages = []
        for i, p in enumerate(reader.pages):
            try:
                pages.append(p.extract_text() or "")
            except Exception as e:
                pages.append(f"[page {i+1} extract error: {e}]")
        return "\n\n".join(pages).strip()
    except Exception as e:
        return f"[pdf download/extract failed: {e}]"


def _is_failed_extract(text) -> bool:
    """_extract_pdf_text 的失败返回值都以 '[pdf ' 开头。"""
    return not text or str(text).startswith("[pdf ")


def _pdf_cache_from_snapshots(limit: int = 3) -> dict:
    """从最近几份快照收集 {url_redacted: text}, 只收抽取成功的。

    周报 PDF 内容不可变(同一 URL 永远同一份文件), 但旧实现每天把全部 200 份
    重新下载 + pypdf 重抽一遍 —— 每天真正新增的只有 1 份。缓存后日常跑一趟
    只需下载新那份, 顺带把"某天网络抖动导致整批抽取失败"的风险压到最小。
    """
    cache: dict = {}
    try:
        snaps = sorted(DATA_DIR.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json"))
    except Exception:
        return cache
    for path in snaps[-limit:]:            # 旧→新, 新的覆盖旧的
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for ch in (data.get("channels") or {}).values():
            for p in (ch.get("pdfs") or []):
                url, text = p.get("url_redacted"), p.get("text")
                if url and not _is_failed_extract(text):
                    cache[url] = text
    return cache


def enrich_pdfs(result, max_pdfs: int = 5, cache: dict = None, extractor=None):
    """Walk channels, extract text for every signed-CDN PDF attachment.

    Stores per-channel `pdfs` as a list (chronological, oldest→newest); the
    splitter pairs each entry with the date marker that introduced it in
    the chat text. Also keeps `latest_pdf` for backward compatibility.

    `cache` = {url_redacted: text}(见 _pdf_cache_from_snapshots), 命中即跳过下载。
    `extractor` 仅供测试注入。
    """
    extract = extractor or _extract_pdf_text
    cache = cache or {}
    for key, ch in result.get("channels", {}).items():
        atts = ch.get("attachments") or []
        # signed CDN URLs only — skip the static Syllabus link
        pdfs = [a for a in atts if a.get("href", "").lower().split("?")[0].endswith(".pdf")
                and "stream-io-cdn" in a.get("href", "")]
        if not pdfs:
            continue
        # cap to avoid runaway downloads; newest are at the end
        pdfs = pdfs[-max_pdfs:]
        extracted = []
        hits = 0
        for i, p in enumerate(pdfs):
            url_redacted = p["href"].split("?")[0]
            cached = cache.get(url_redacted)
            if not _is_failed_extract(cached):      # 失败的缓存条目要重抽, 别让错误粘住
                text = cached
                hits += 1
            else:
                print(f"[info] extracting PDF [{i+1}/{len(pdfs)}] for {key}: {p.get('name')}",
                      file=sys.stderr)
                text = extract(p["href"])
            extracted.append({
                "name": p.get("name"),
                "url_redacted": url_redacted,
                "text": text,
                "text_chars": len(text),
            })
        if hits:
            print(f"[info] {key}: {hits}/{len(pdfs)} PDF 命中缓存, 只下载了 {len(pdfs) - hits} 份",
                  file=sys.stderr)
        ch["pdfs"] = extracted
        ch["latest_pdf"] = extracted[-1]  # back-compat
    return result


def save(result):
    today = datetime.now().strftime("%Y-%m-%d")
    out_path = DATA_DIR / f"{today}.json"

    has_text = any(ch.get("text") for ch in result.get("channels", {}).values())
    if not has_text:
        # 没拿到任何正文 = 本次抓取失败(最常见原因: benzinga 登录 cookie 过期,
        # 整个 chat 页被重定向到 login, 侧边栏渲染不出来 → 点频道超时)。
        # 绝不落盘: PWA 的闸门是"今天已有快照就不再抓"(optionstrat_server.py
        # `snapshot_exists`), 一旦把这份垃圾写成当天文件, 就算 cookie 当天修好了
        # 也整天不会重试, pre-market 静默停更。
        if out_path.exists() and out_path.stat().st_size > 1000:
            print(f"[warn] all channels empty/errored — refusing to overwrite "
                  f"existing {out_path.name} ({out_path.stat().st_size:,} bytes)",
                  file=sys.stderr)
            return out_path
        print(f"[err] all channels empty/errored — refusing to create {out_path.name}; "
              f"检查 benzinga 登录态(cookies.json 是否过期)", file=sys.stderr)
        return None

    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channels", nargs="*", default=None)
    ap.add_argument("--headed", action="store_true", help="run with visible browser")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    print(f"[info] cookies file: {COOKIES_FILE}", file=sys.stderr)
    print(f"[info] data dir:     {DATA_DIR}", file=sys.stderr)
    # 过期/临期自动调 puppeteer stealth 重新登录
    _ensure_cookies_valid()

    result = fetch_channels(channel_keys=args.channels, headless=not args.headed, debug=args.debug)
    pdf_cache = _pdf_cache_from_snapshots()
    if pdf_cache:
        print(f"[info] PDF 正文缓存: {len(pdf_cache)} 份可复用(内容不可变, 只下载新增的)",
              file=sys.stderr)
    result = enrich_pdfs(result, cache=pdf_cache)
    out = save(result)
    if out is None:
        # 抓取彻底失败且没有当天快照可用。以非 0 退出, 让 _autofetch.log 里的
        # "fetcher exited with N" 直接暴露失败(以前恒为 0, 失败完全看不出来)。
        for k, v in result.get("channels", {}).items():
            print(f"=== {k} === ERROR: {v.get('error', 'no text')}")
        sys.exit(2)
    print(f"[info] saved → {out}", file=sys.stderr)

    # also split into per-date files for the history view
    try:
        from splitter import split_snapshot  # type: ignore
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from splitter import split_snapshot
    try:
        written = split_snapshot(out)
        print(f"[info] history: wrote {len(written)} per-date files", file=sys.stderr)
    except Exception as e:
        print(f"[warn] splitter failed: {e}", file=sys.stderr)

    # quick summary to stdout
    for k, v in result["channels"].items():
        if "error" in v:
            print(f"\n=== {k} === ERROR: {v['error']}")
        else:
            txt = v["text"]
            print(f"\n=== {k} === ({len(txt)} chars, {len(v.get('attachments', []))} attachments)")
            print(txt[:500])
            print("..." if len(txt) > 500 else "")


if __name__ == "__main__":
    main()
