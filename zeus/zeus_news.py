"""Free news wire for the Digest: Finviz + WSJ + MarketWatch, bilingual.

  WSJ          public Dow Jones RSS (Markets, Business, Tech, World)
  MarketWatch  public Dow Jones RSS (Top stories, Bulletins)
  Finviz       finviz.com/news (free page, polite single request) or, with a
               Finviz Elite token, the official news export

Headlines and teasers are translated to Chinese with the same translation chain
the Benzinga digest uses, then merged into the Digest's bilingual store, so the
"Today" and "Calendar" views, source chips, read/unread, likes and AI summaries
all work for these sources exactly as they do for Benzinga.

Full articles open on the publisher's site in your browser (your own
subscription, if you have one). Nothing is republished.
"""
from __future__ import annotations

import csv
import io
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urlsplit

import zeus_config as hc

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = timezone(timedelta(hours=-5))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

WSJ_FEEDS = {
    "Markets": "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
    "Business": "https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness",
    "Tech": "https://feeds.content.dowjones.io/public/rss/RSSWSJD",
    "World": "https://feeds.content.dowjones.io/public/rss/RSSWorldNews",
}
MW_FEEDS = {
    "Top Stories": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "Bulletins": "https://feeds.content.dowjones.io/public/rss/mw_bulletins",
}
FINVIZ_FREE = "https://finviz.com/news.ashx"
FINVIZ_ELITE = "https://elite.finviz.com/news_export.ashx?v=1&auth={token}"
KEEP_DAYS = 30
_last_status: dict = {}


def _get(url: str, timeout: int = 20) -> str:
    import requests
    r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=timeout)
    r.raise_for_status()
    return r.text


def _clean(s: str | None) -> str:
    s = unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _drop_tracking(link: str) -> str:
    """WSJ and MarketWatch links carry tracking parameters: drop the query string for those
    hosts only, judged by the host name rather than by text anywhere in the URL."""
    host = (urlsplit(link).hostname or "").lower()
    if host in ("wsj.com", "marketwatch.com") or host.endswith((".wsj.com", ".marketwatch.com")):
        return link.split("?")[0]
    return link


def _rss(url: str, source: str, section: str) -> list[dict]:
    xml = _get(url)
    root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    out = []
    for it in root.iter("item"):
        title = _clean(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        pub = it.findtext("pubDate") or ""
        try:
            dt = parsedate_to_datetime(pub)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            dt = datetime.now(timezone.utc)
        out.append({"url": _drop_tracking(link),
                    "title": title, "teaser": _clean(it.findtext("description"))[:500],
                    "source": source, "section": section, "ts": _ms(dt),
                    "date": dt.astimezone(_ET).strftime("%Y-%m-%d")})
    return out


def fetch_wsj() -> list[dict]:
    out = []
    for sec, url in WSJ_FEEDS.items():
        try:
            out += _rss(url, "WSJ", sec)
        except Exception as e:
            _last_status["WSJ:" + sec] = f"error: {e}"
    return out


def fetch_marketwatch() -> list[dict]:
    out = []
    for sec, url in MW_FEEDS.items():
        try:
            out += _rss(url, "MarketWatch", sec)
        except Exception as e:
            _last_status["MarketWatch:" + sec] = f"error: {e}"
    return out


_FV_ROW = re.compile(r'<tr[^>]*news_table-row[^>]*>(.*?)</tr>', re.S)
_FV_DATE = re.compile(r'news_date-cell[^>]*>\s*([^<]+?)\s*<')
_FV_LINK = re.compile(r'<a href="([^"]+)"[^>]*class="nn-tab-link"[^>]*>(.*?)</a>', re.S)
_FV_SRC = re.compile(r'icons_news[^#"]*#([a-z0-9\-]+?)-(?:light|dark)')


def _finviz_time(txt: str, now_et: datetime) -> datetime:
    txt = txt.strip()
    m = re.match(r"^(\d{1,2}):(\d{2})(AM|PM)$", txt, re.I)
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3).upper()
        h = (h % 12) + (12 if ap == "PM" else 0)
        dt = now_et.replace(hour=h, minute=mi, second=0, microsecond=0)
        if dt > now_et + timedelta(minutes=5):
            dt -= timedelta(days=1)
        return dt
    m = re.match(r"^([A-Za-z]{3})-(\d{1,2})(?:-(\d{2,4}))?$", txt)
    if m:
        mon = datetime.strptime(m.group(1), "%b").month
        year = now_et.year
        dt = now_et.replace(month=mon, day=int(m.group(2)), hour=12, minute=0, second=0, microsecond=0)
        if dt > now_et + timedelta(days=1):
            dt = dt.replace(year=year - 1)
        return dt
    return now_et


def fetch_finviz() -> list[dict]:
    token = (hc.get("finviz_elite_token") or "").strip()
    now_et = datetime.now(_ET)
    if token:
        text = _get(FINVIZ_ELITE.format(token=token))
        out = []
        for row in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
            r = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            title, url = r.get("title"), r.get("url") or r.get("link")
            if not title or not url:
                continue
            try:
                dt = datetime.strptime(r.get("date", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=_ET)
            except Exception:
                dt = now_et
            out.append({"url": url, "title": title, "teaser": "", "source": "Finviz",
                        "section": r.get("source") or r.get("category") or "Finviz Elite",
                        "ts": _ms(dt), "date": dt.strftime("%Y-%m-%d")})
        return out
    html = _get(FINVIZ_FREE)
    out = []
    for row in _FV_ROW.findall(html):
        lm = _FV_LINK.search(row)
        if not lm:
            continue
        url, title = lm.group(1).strip(), _clean(lm.group(2))
        if not url.startswith("http"):
            url = "https://finviz.com" + url
        dm = _FV_DATE.search(row)
        dt = _finviz_time(dm.group(1), now_et) if dm else now_et
        sm = _FV_SRC.search(row)
        pub = (sm.group(1).replace("-", " ").title() if sm else "")
        out.append({"url": url, "title": title, "teaser": "", "source": "Finviz", "section": pub or "Finviz",
                    "ts": _ms(dt), "date": dt.strftime("%Y-%m-%d")})
    return out


FETCHERS = {"finviz": fetch_finviz, "wsj": fetch_wsj, "marketwatch": fetch_marketwatch}


def enabled_sources() -> list[str]:
    raw = str(hc.get("free_news_sources") or "")
    return [s.strip().lower() for s in raw.split(",") if s.strip().lower() in FETCHERS]


def _translate(texts: list[str]) -> list[str]:
    mode = str(hc.get("news_translate") or "google").lower()
    if mode == "off" or not texts:
        return [""] * len(texts)
    if mode == "ai":
        try:
            import zeus_ai
            if zeus_ai.available():
                joined = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
                out, _, err = zeus_ai.complete(
                    "Translate each numbered English financial headline into concise Simplified Chinese. "
                    "Answer with the same numbering, one per line, nothing else.", joined, model="haiku")
                if out and not err:
                    lines = {}
                    for ln in out.splitlines():
                        m = re.match(r"^\s*(\d+)[.)、]\s*(.+)$", ln)
                        if m:
                            lines[int(m.group(1))] = m.group(2).strip()
                    if len(lines) >= len(texts) * 0.8:
                        return [lines.get(i + 1, "") for i in range(len(texts))]
        except Exception:
            pass
    try:
        from benzinga_mcp.digest import translate_lines
        res = translate_lines(texts)
        return [(z if z and z != t else "") for z, t in zip(res, texts)]
    except Exception:
        return [""] * len(texts)


def refresh(state: dict) -> tuple[dict, dict]:
    """Fetch enabled sources and merge into the bilingual `state` (same schema
    the Digest reads from /api/bilingual/data). Returns (new_state, report)."""
    report = {}
    have = {a.get("url"): a for a in (state.get("articles") or []) if isinstance(a, dict) and a.get("url")}
    fresh: list[dict] = []
    for src in enabled_sources():
        try:
            items = FETCHERS[src]()
            report[src] = len(items)
            _last_status[src] = f"ok {len(items)} @ {datetime.now().strftime('%H:%M')}"
            fresh += items
        except Exception as e:
            report[src] = f"error: {e}"
            _last_status[src] = f"error: {e}"
    new = [a for a in fresh if a["url"] not in have]
    # dedupe by url within this batch
    seen_urls, uniq = set(), []
    for a in new:
        if a["url"] in seen_urls:
            continue
        seen_urls.add(a["url"])
        uniq.append(a)
    if uniq:
        zh_titles = _translate([a["title"] for a in uniq])
        teasers = [a for a in uniq if a.get("teaser")]
        zh_teasers = _translate([a["teaser"] for a in teasers]) if teasers else []
        for a, z in zip(uniq, zh_titles):
            a["titleZh"] = z
        for a, z in zip(teasers, zh_teasers):
            a["teaserZh"] = z
    for a in uniq:
        have[a["url"]] = a
    cutoff = _ms(datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS))
    arts = sorted((a for a in have.values() if int(a.get("ts") or 0) >= cutoff),
                  key=lambda a: -int(a.get("ts") or 0))
    keep = {a["url"] for a in arts}
    out = dict(state)
    out["articles"] = arts
    out["sections"] = sorted({a.get("source", "") for a in arts if a.get("source")})
    for k in ("seen", "saved", "deleted"):
        m = dict(out.get(k) or {})
        out[k] = {u: v for u, v in m.items() if u in keep or k == "saved"}
    out["updatedAt"] = int(time.time() * 1000)
    report["new"] = len(uniq)
    report["total"] = len(arts)
    return out, report


def status() -> dict:
    return {"sources": enabled_sources(), "last": dict(_last_status),
            "finviz_mode": "elite" if hc.get("finviz_elite_token") else "free"}
