"""SEC EDGAR filings — ticker → CIK → recent 10-Q/10-K, plus a same-origin
document proxy so the HTML can be iframed (sec.gov sends X-Frame-Options:
SAMEORIGIN, which blocks direct cross-origin embedding).

Endpoints:
  GET /api/sec/filings?ticker=AAPL&forms=10-Q,10-K&limit=4
      → recent filings list (form, dates, accession, primary doc, proxy URL)
  GET /api/sec/document?cik=..&accession=..&doc=..
      → the filing's primary .htm, with a <base href> injected so its relative
        images/anchors resolve against sec.gov. Returned same-origin → iframable.

SEC requires a descriptive User-Agent on every request (else 403) and asks for
<= 10 req/s. We cache the ticker→CIK map and per-CIK submissions in memory.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# SEC wants a real contact in the UA. Not a secret — it's their published rule.
import sys as _sys_hc
_sys_hc.path.insert(0, str(Path(__file__).resolve().parents[1]))
import zeus_config as _hc  # noqa: E402
_UA = str(_hc.get("sec_user_agent") or "ZEUS research contact@example.com")  # SEC wants a contact e-mail; set yours in Settings
_TIMEOUT = 20

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
_ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"

# ─── HTTP helper ─────────────────────────────────────────────────────────────

def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept-Encoding": "gzip, deflate"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        return raw


# ─── ticker → CIK map (cached) ───────────────────────────────────────────────

_cik_map: dict[str, str] = {}        # "AAPL" -> "320193" (no leading zeros)
_cik_lock = threading.Lock()
_cik_loaded_at = 0.0
_CIK_TTL = 24 * 3600


def _load_cik_map() -> dict[str, str]:
    global _cik_map, _cik_loaded_at
    with _cik_lock:
        if _cik_map and (time.time() - _cik_loaded_at) < _CIK_TTL:
            return _cik_map
        import json
        data = json.loads(_fetch(_TICKERS_URL))
        m: dict[str, str] = {}
        # data is {"0": {"cik_str": 320193, "ticker": "AAPL", "title": ...}, ...}
        for row in data.values():
            t = str(row.get("ticker", "")).upper()
            if t:
                m[t] = str(row.get("cik_str"))
        _cik_map = m
        _cik_loaded_at = time.time()
        return _cik_map


def _ticker_to_cik(ticker: str) -> Optional[str]:
    return _load_cik_map().get(ticker.upper())


# ─── submissions (cached per CIK, short TTL) ─────────────────────────────────

_subs_cache: dict[str, tuple[float, dict]] = {}
_SUBS_TTL = 3600


def _get_submissions(cik: str) -> dict:
    now = time.time()
    hit = _subs_cache.get(cik)
    if hit and (now - hit[0]) < _SUBS_TTL:
        return hit[1]
    import json
    url = _SUBMISSIONS_URL.format(cik10=cik.zfill(10))
    data = json.loads(_fetch(url))
    _subs_cache[cik] = (now, data)
    return data


# ─── router ──────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/sec", tags=["sec"])

_ACC_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")        # 0000320193-26-000013
_ACC_NODASH_RE = re.compile(r"^\d{18}$")
_DOC_RE = re.compile(r"^[A-Za-z0-9._-]+\.(htm|html)$")  # primary doc filename only


@router.get("/filings")
def filings(ticker: str = Query(..., min_length=1, max_length=10),
            forms: str = Query("10-Q,10-K"),
            limit: int = Query(4, ge=1, le=60)):
    want = {f.strip().upper() for f in forms.split(",") if f.strip()}
    cik = _ticker_to_cik(ticker)
    if not cik:
        raise HTTPException(status_code=404, detail=f"未找到 ticker «{ticker.upper()}» 对应的 SEC CIK")
    try:
        subs = _get_submissions(cik)
    except Exception as e:  # network / SEC error
        raise HTTPException(status_code=502, detail=f"SEC submissions 拉取失败: {e}")

    recent = subs.get("filings", {}).get("recent", {})
    form = recent.get("form", [])
    acc = recent.get("accessionNumber", [])
    doc = recent.get("primaryDocument", [])
    fdate = recent.get("filingDate", [])
    rdate = recent.get("reportDate", [])
    desc = recent.get("primaryDocDescription", [])

    out = []
    for i in range(len(form)):
        if want and form[i].upper() not in want:
            continue
        primary = doc[i] if i < len(doc) else ""
        if not primary.lower().endswith((".htm", ".html")):
            continue  # skip non-HTML primary docs (rare old filings)
        out.append({
            "form": form[i],
            "filing_date": fdate[i] if i < len(fdate) else "",
            "report_date": rdate[i] if i < len(rdate) else "",
            "accession": acc[i] if i < len(acc) else "",
            "primary_document": primary,
            "description": desc[i] if i < len(desc) else "",
            "cik": cik,
        })
        if len(out) >= limit:
            break

    return {
        "ticker": ticker.upper(),
        "cik": cik,
        "company": subs.get("name", ""),
        "count": len(out),
        "filings": out,
    }


@router.get("/document", response_class=HTMLResponse)
def document(cik: str = Query(...), accession: str = Query(...), doc: str = Query(...)):
    # Strict validation — these go straight into a sec.gov URL, so guard against
    # SSRF / path traversal. Only digits + a known accession/filename shape.
    if not cik.isdigit():
        raise HTTPException(status_code=400, detail="bad cik")
    acc_nodash = accession.replace("-", "")
    if not (_ACC_RE.match(accession) or _ACC_NODASH_RE.match(acc_nodash)):
        raise HTTPException(status_code=400, detail="bad accession")
    if not _DOC_RE.match(doc):
        raise HTTPException(status_code=400, detail="bad document name")

    base = _ARCHIVE_BASE.format(cik=cik, acc=acc_nodash)
    url = base + doc
    try:
        html = _fetch(url).decode("utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"SEC 文档拉取失败: {e}")

    # Inject <base> so the document's relative images/CSS resolve against sec.gov
    # even though we're serving it from our own origin. The <base> also makes the
    # doc's own "#anchor" links (its embedded TOC / cross-refs) resolve to sec.gov
    # URLs — clicking one would navigate the iframe cross-origin and sec.gov's
    # X-Frame-Options gives "refused to connect". So also inject a click handler:
    # "#" links scroll within the iframe instead; any other link opens in a new tab.
    base_tag = f'<base href="{base}">'
    fix_js = (
        '<script>(function(){document.addEventListener("click",function(e){'
        'var a=e.target&&e.target.closest?e.target.closest("a"):null;if(!a)return;'
        'var h=a.getAttribute("href");if(!h)return;'
        'if(h.charAt(0)==="#"){e.preventDefault();'
        'var id=h.slice(1),t=document.getElementById(id)||document.getElementsByName(id)[0];'
        'if(t)t.scrollIntoView();}'
        'else if(/^https?:/i.test(a.href)){e.preventDefault();window.open(a.href,"_blank","noopener");}'
        '},true);})();</script>'
    )
    inject = base_tag + fix_js
    if re.search(r"<head[^>]*>", html, flags=re.IGNORECASE):
        html = re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + inject, html, count=1, flags=re.IGNORECASE)
    elif re.search(r"<html[^>]*>", html, flags=re.IGNORECASE):
        html = re.sub(r"(<html[^>]*>)", lambda m: m.group(1) + "<head>" + inject + "</head>", html, count=1, flags=re.IGNORECASE)
    else:
        html = inject + html

    return HTMLResponse(content=html)


@router.get("/docs")
def docs(cik: str = Query(...), accession: str = Query(...)):
    """List a filing's HTML documents (primary + exhibits), biggest first.
    8-K earnings content lives in the EX-99.1 exhibit (much bigger than the thin
    cover), so the frontend can default-open the biggest doc and let the user
    switch. Excludes XBRL viewer renders (R*.htm) and the filing index pages."""
    if not cik.isdigit():
        raise HTTPException(status_code=400, detail="bad cik")
    acc_nodash = accession.replace("-", "")
    if not (_ACC_RE.match(accession) or _ACC_NODASH_RE.match(acc_nodash)):
        raise HTTPException(status_code=400, detail="bad accession")
    base = _ARCHIVE_BASE.format(cik=cik, acc=acc_nodash)
    try:
        data = json.loads(_fetch(base + "index.json"))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"SEC index 拉取失败: {e}")
    out = []
    for it in data.get("directory", {}).get("item", []):
        nm = it.get("name", "")
        low = nm.lower()
        if not low.endswith((".htm", ".html")):
            continue
        if re.match(r"r\d+\.htm", low):                    # XBRL viewer renders
            continue
        if re.search(r"-index(-headers)?\.html?$", low):   # filing index pages
            continue
        try:
            size = int(it.get("size") or 0)
        except Exception:
            size = 0
        out.append({"name": nm, "size": size})
    out.sort(key=lambda d: d["size"], reverse=True)        # biggest = the content doc
    return {"cik": cik, "accession": accession, "docs": out}


@router.get("/transcript", response_class=HTMLResponse)
def transcript(ticker: str = Query(..., min_length=1, max_length=10),
               refresh: int = Query(0)):
    """Earnings-call transcript for the SEC pane's 📞 button. SEC/Benzinga don't
    host transcripts; we pull them from YouTube (yt-dlp) and render an HTML page
    that loads in the same iframe so the bilingual translator just works."""
    if not re.match(r"^[A-Za-z.]{1,10}$", ticker):
        raise HTTPException(status_code=400, detail="bad ticker")
    from sec_filings.transcript import get_transcript_html
    try:
        html = get_transcript_html(ticker, refresh=bool(refresh))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"transcript 获取失败: {e}")
    return HTMLResponse(content=html)


# ─── bilingual translation cache (per-ticker, on-disk) ───────────────────────
# The bilingual SEC viewer walks the filing's visible, non-table, non-XBRL text
# blocks and posts them here as the user scrolls. Misses go through the shared
# Google→qwen pipeline (benzinga_mcp.digest.translate_lines); results are cached
# on disk keyed by text hash, one JSON file per ticker — so the user can open
# the folder (POST /api/sec/cache/open) and delete unwanted tickers' caches.

_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "sec_tr_cache"
_tr_mem: dict[str, dict] = {}
_tr_lock = threading.Lock()


def _tr_norm(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


def _tr_key(t: str) -> str:
    return hashlib.sha1(_tr_norm(t).encode("utf-8")).hexdigest()


def _tr_file(ticker: str) -> Path:
    safe = re.sub(r"[^A-Z0-9._-]", "", (ticker or "").upper()) or "_"
    return _CACHE_DIR / f"{safe}.json"


def _tr_load(ticker: str) -> dict:
    with _tr_lock:
        if ticker in _tr_mem:
            return _tr_mem[ticker]
        d: dict = {}
        f = _tr_file(ticker)
        if f.exists():
            try:
                d = json.loads(f.read_text("utf-8"))
            except Exception:
                d = {}
        _tr_mem[ticker] = d
        return d


def _tr_save(ticker: str, data: dict) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f = _tr_file(ticker)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    os.replace(tmp, f)        # atomic; server is single-process


def _translate_lines(texts: list) -> list:
    """Lazy import of the shared digest translator (Google→qwen)."""
    import sys
    src = str(Path(__file__).resolve().parent.parent / "benzinga_digest" / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from benzinga_mcp.digest import translate_lines
    return translate_lines(texts)


def _zh_looks_english(en_text: str, zh_text: str) -> bool:
    """检测缓存的"中文"翻译是否实际是英文（英翻英），需要重翻。"""
    if not zh_text or len(zh_text.strip()) < 10:
        return False
    if " ".join(zh_text.split()).lower() == " ".join(en_text.split()).lower():
        return True
    cjk = sum(1 for c in zh_text if ("一" <= c <= "鿿" or "㐀" <= c <= "䶿"))
    if cjk == 0:
        return True
    if len(zh_text) > 20 and cjk / len(zh_text) < 0.1:
        return True
    return False


class _TrReq(BaseModel):
    ticker: str = ""
    texts: list[str] = []
    force: bool = False        # true → skip cache, re-translate & overwrite (reload button)


@router.post("/translate")
def translate(req: _TrReq):
    texts = req.texts or []
    ticker = (req.ticker or "_").upper()
    if not texts:
        return {"zh": []}
    cache = _tr_load(ticker)
    out: list = [None] * len(texts)
    miss_idx: list = []
    miss_txt: list = []
    for i, t in enumerate(texts):
        if req.force:
            miss_idx.append(i)
            miss_txt.append(t)
            continue
        hit = cache.get(_tr_key(t))
        if hit and hit.get("zh") and not _zh_looks_english(t, hit["zh"]):
            out[i] = hit["zh"]
        else:
            miss_idx.append(i)
            miss_txt.append(t)
    if miss_txt:
        try:
            zhs = _translate_lines(miss_txt)
        except Exception:
            zhs = None
        dirty = False
        for j, i in enumerate(miss_idx):
            zh = zhs[j] if (zhs and j < len(zhs) and zhs[j]) else ""
            if _zh_looks_english(texts[i], zh):
                zh = ""
            out[i] = zh
            if zh:
                cache[_tr_key(texts[i])] = {"en": _tr_norm(texts[i])[:400], "zh": zh}
                dirty = True
        if dirty:
            with _tr_lock:
                _tr_save(ticker, cache)
    return {"zh": [x or "" for x in out], "cached": len(texts) - len(miss_txt)}


@router.post("/translate/lookup")
def translate_lookup(req: _TrReq):
    """Cache-only lookup — misses come back as "" (no translation call).
    Front-end uses this in a first pass to instantly fill in whatever's already
    cached, then queues misses for the real /translate endpoint. Opens a filing
    "instantly" for previously-viewed docs; new blocks fill in as they translate.
    """
    texts = req.texts or []
    ticker = (req.ticker or "_").upper()
    if not texts:
        return {"zh": [], "hits": []}
    cache = _tr_load(ticker)
    zh_out: list = []
    hits: list = []
    for t in texts:
        hit = cache.get(_tr_key(t))
        if hit and hit.get("zh") and not _zh_looks_english(t, hit["zh"]):
            zh_out.append(hit["zh"])
            hits.append(True)
        else:
            zh_out.append("")
            hits.append(False)
    return {"zh": zh_out, "hits": hits, "cached": sum(1 for h in hits if h)}


@router.post("/cache/open")
def cache_open():
    """Reveal the on-disk translation cache folder in Finder so the user can
    delete unwanted tickers' cached translations."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.Popen(["open", str(_CACHE_DIR)])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"无法打开缓存文件夹: {e}")
    return {"ok": True, "path": str(_CACHE_DIR)}
