"""YouTube earnings-call transcript for the SEC pane's 📞 button.

SEC and Benzinga don't host earnings-call transcripts (Benzinga only has the
conference-call *schedule*). We pull them from YouTube via the yt-dlp CLI:
search "{ticker} earnings call", pick the longest recent match, download the
English auto-captions (json3), clean them into paragraphs, and render an HTML
page that loads in the same #secFrame iframe — so the existing bilingual
translator walks it unchanged. Cached per ticker on disk.

Caveats: YouTube auto-captions have recognition errors and aren't official; not
every company/quarter has a call on YouTube. yt-dlp solves YouTube's JS
challenges via deno (must be on PATH).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "sec_tr_cache" / "transcripts"
_FRESH_SECS = 2 * 86400          # re-search if cache older than 2 days (picks up new quarters)


def _yt_dlp() -> str:
    for c in (shutil.which("yt-dlp"), "/opt/homebrew/bin/yt-dlp", "/usr/local/bin/yt-dlp"):
        if c and os.path.exists(c):
            return c
    raise RuntimeError("yt-dlp 未安装")


def _esc(s) -> str:
    return (str(s if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


# ─── search + pick the best earnings-call video ──────────────────────────────

def _search(ticker: str, yt: str) -> list:
    q = f"ytsearch6:{ticker} earnings call"
    out = subprocess.run(
        [yt, q, "--skip-download", "--no-warnings", "--no-playlist",
         "--print", "%(id)s\t%(title)s\t%(duration)s\t%(upload_date)s"],
        capture_output=True, text=True, timeout=90)
    cands = []
    for line in out.stdout.splitlines():
        p = line.split("\t")
        if len(p) < 4 or not p[0]:
            continue
        try:
            dur = int(float(p[2]))
        except Exception:
            dur = 0
        cands.append({"id": p[0], "title": p[1], "dur": dur, "up": p[3]})
    return cands


def _pick(cands: list, ticker: str):
    tl = ticker.lower()

    def score(c):
        t = (c["title"] or "").lower()
        s = 0
        if "earnings" in t:
            s += 3
        if "call" in t or "conference" in t:
            s += 2
        if tl in t.split() or ("$" + tl) in t:
            s += 2
        if 1200 <= c["dur"] <= 7800:            # full call ≈ 20–130 min
            s += 3
        elif c["dur"] < 600:                    # clip / news segment
            s -= 3
        return (s, c.get("up", ""))             # tie-break: newer upload

    pool = [c for c in cands if c["dur"] >= 900] or cands
    return max(pool, key=score) if pool else None


# ─── caption download + clean ────────────────────────────────────────────────

def _evtext(ev: dict) -> str:
    b = ""
    for sg in ev.get("segs") or []:
        u = sg.get("utf8", "")
        if not u:
            continue
        # captions drop the space at line wraps → re-insert when a word would glue
        if b and not b[-1].isspace() and u[0] not in " \n.,!?;:)'\"]%":
            b += " "
        b += u
    return b


def _paragraphs(events: list) -> list:
    paras, cur, last_t = [], "", None
    for ev in events:
        if not ev.get("segs"):
            continue
        t = ev.get("tStartMs", 0)
        tx = _evtext(ev)
        if not tx.strip():
            continue
        if last_t is not None and ((t - last_t > 1400 and len(cur) > 180) or len(cur) > 360):
            paras.append(cur.strip())
            cur = ""
        if cur and not cur[-1].isspace():
            cur += " "
        cur += tx
        last_t = t
    if cur.strip():
        paras.append(cur.strip())
    return [re.sub(r"\s+", " ", p).strip() for p in paras if len(p.strip()) > 2]


def _fetch_captions(vid: str, yt: str) -> list:
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(
            [yt, f"https://www.youtube.com/watch?v={vid}", "--skip-download",
             "--write-subs", "--write-auto-subs", "--sub-langs", "en.*",
             "--sub-format", "json3", "--no-warnings", "-o", os.path.join(td, "%(id)s")],
            capture_output=True, text=True, timeout=150)
        files = [f for f in os.listdir(td) if f.endswith(".json3")]
        if not files:
            return []
        # prefer plain "en" over "en-orig"/translations when present
        files.sort(key=lambda f: (".en." not in f, f))
        data = json.loads(Path(os.path.join(td, files[0])).read_text("utf-8"))
    return _paragraphs(data.get("events", []))


# ─── render ──────────────────────────────────────────────────────────────────

def _render(ticker: str, pick: dict, paras: list) -> str:
    src = f"https://www.youtube.com/watch?v={pick['id']}"
    up = pick.get("up", "") or ""
    date = f"{up[:4]}-{up[4:6]}-{up[6:8]}" if len(up) == 8 else ""
    body = "\n".join(f'<div class="tr-p">{_esc(p)}</div>' for p in paras)
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<title>{_esc(ticker)} earnings call</title>'
        '<style>body{font-family:-apple-system,system-ui,sans-serif;max-width:820px;'
        'margin:0 auto;padding:22px 28px;color:#1a1a1a;line-height:1.65;background:#fff}'
        'h1{font-size:19px;margin:0 0 4px}.meta{color:#666;font-size:13px;margin-bottom:18px}'
        '.meta a{color:#1a73e8}.tr-p{margin:11px 0;font-size:15px}'
        '.note{color:#999;font-size:12px;margin-top:26px;border-top:1px solid #eee;padding-top:10px}</style>'
        '</head><body>'
        f'<h1>{_esc(ticker)} · 财报电话会 transcript</h1>'
        f'<div class="meta">{_esc(pick.get("title"))} · {_esc(date)} · 来源 '
        f'<a href="{src}" target="_blank" rel="noopener">YouTube ↗</a> · 自动字幕</div>'
        f'{body}'
        '<div class="note">字幕由 YouTube 自动生成，可能有识别误差，非官方记录。'
        'SEC / Benzinga 不提供电话会文字记录。</div>'
        '</body></html>')


def _err(ticker: str, msg: str) -> str:
    return (
        '<!doctype html><html><head><meta charset="utf-8"></head>'
        '<body style="font-family:system-ui;padding:40px;color:#333;background:#fff">'
        f'<h2>{_esc(ticker)} · 电话会 transcript</h2>'
        f'<p style="color:#a00">{_esc(msg)}</p>'
        '<p style="color:#888;font-size:13px">SEC / Benzinga 不提供电话会文字记录；本功能从 '
        'YouTube 抓取，不是每家公司每季都有，或视频暂无英文字幕。</p></body></html>')


def get_transcript_html(ticker: str, refresh: bool = False) -> str:
    ticker = ticker.upper()
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = _CACHE_DIR / f"{re.sub(r'[^A-Z0-9._-]', '', ticker) or '_'}.html"
    if cache.exists() and not refresh and (time.time() - cache.stat().st_mtime < _FRESH_SECS):
        return cache.read_text("utf-8")
    yt = _yt_dlp()
    pick = _pick(_search(ticker, yt), ticker)
    if not pick:
        return _err(ticker, "没找到该公司的财报电话会视频")
    paras = _fetch_captions(pick["id"], yt)
    if not paras:
        return _err(ticker, f"找到视频《{pick.get('title')}》但没有可用的英文字幕")
    html = _render(ticker, pick, paras)
    try:
        cache.write_text(html, "utf-8")
    except Exception:
        pass
    return html
