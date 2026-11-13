"""公司简介生成器——从最新 10-Q 的 MD&A + Risk Factors 用 Haiku 生成中文摘要。

缓存在 data/company_profiles.json, 只在作者手动点「更新」时重新生成。
批量生成: python -m narratives.company_profile --all
"""
from __future__ import annotations

import json, os, re, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_REPO = Path(__file__).resolve().parent.parent
CACHE_FILE = _REPO / "data" / "company_profiles.json"

# SEC EDGAR constants
_SEC_BASE = "https://efts.sec.gov/LATEST/search-index"
_EDGAR_SUB = "https://data.sec.gov/submissions/CIK{cik}.json"
_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"
_UA = "Mozilla/5.0 (ZEUS research)"
_HEADERS = {"User-Agent": _UA}

import urllib.request, urllib.error


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_cache(data: dict):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                          encoding="utf-8")


def get_profile(ticker: str) -> dict | None:
    cache = _load_cache()
    return cache.get(ticker.upper())


def _ticker_to_cik(ticker: str) -> str | None:
    """Use SEC company_tickers.json to map ticker → CIK (zero-padded 10 digits)."""
    url = "https://www.sec.gov/files/company_tickers.json"
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception:
        return None
    tk = ticker.upper()
    # SEC uses '-' not '.' (e.g. BRK-B not BRK.B)
    candidates = [tk]
    if '.' in tk:
        candidates.append(tk.replace('.', '-'))
    for v in data.values():
        vtk = str(v.get("ticker", "")).upper()
        if vtk in candidates:
            return str(v.get("cik_str", "")).zfill(10)
    return None


def _get_latest_10q(cik: str) -> dict | None:
    """Fetch most recent 10-Q filing metadata from SEC EDGAR."""
    url = _EDGAR_SUB.format(cik=cik)
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            subs = json.loads(r.read())
    except Exception:
        return None
    recent = subs.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accs = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    fdates = recent.get("filingDate", [])
    for i, f in enumerate(forms):
        if f.upper() == "10-Q" and i < len(docs):
            doc = docs[i]
            if not doc.lower().endswith((".htm", ".html")):
                continue
            return {
                "cik": cik,
                "accession": accs[i],
                "doc": doc,
                "filing_date": fdates[i] if i < len(fdates) else "",
                "company": subs.get("name", ""),
            }
    return None


def _fetch_filing_html(cik: str, accession: str, doc: str) -> str:
    """Fetch the actual filing HTML from SEC archives."""
    acc_nodash = accession.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{doc}"
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def _extract_sections(html: str) -> dict:
    """Extract MD&A and Risk Factors text from 10-Q HTML.
    Returns {mda: str, risk: str} with plain text (tags stripped)."""
    # Strip style/script, then all HTML tags → plain text
    clean = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.I | re.S)
    clean = re.sub(r'<script[^>]*>.*?</script>', '', clean, flags=re.I | re.S)
    clean = re.sub(r'<[^>]+>', ' ', clean)
    clean = re.sub(r'&[a-zA-Z]+;', ' ', clean)
    clean = re.sub(r'&#\d+;', ' ', clean)
    full_text = re.sub(r'\s+', ' ', clean).strip()

    def _find_section(header_pat, end_pats, min_len=2000):
        """Find the real section by picking the longest candidate.
        TOC entries and cross-references are short; the actual section is longest."""
        best, best_len = "", 0
        for m in re.finditer(header_pat, full_text, re.I):
            start = m.start()
            end = len(full_text)
            for epat in end_pats:
                em = re.search(epat, full_text[start + 80:], re.I)
                if em:
                    end = start + 80 + em.start()
                    break
            length = end - start
            if length > best_len:
                best_len = length
                best = full_text[start:end].strip()
        return best if best_len >= min_len else ""

    mda = _find_section(
        r"Management.s Discussion and Analysis",
        [r"Item\s*3\.?\s*(?:—|–|-|\.|\s)\s*Quantitative",
         r"QUANTITATIVE AND QUALITATIVE",
         r"Item\s*3\."]
    )
    risk = _find_section(
        r"Item\s*1A\.?\s*(?:—|–|-|\.|\s)\s*Risk\s*Factor",
        [r"Item\s*(?:1B|2)\.?\s*(?:—|–|-|\.)",
         r"UNRESOLVED STAFF COMMENTS",
         r"Item\s*2\.?\s*(?:—|–|-|\.|\s)\s*(?:Unregistered|Properties)"]
    )
    return {"mda": mda, "risk": risk}


def _truncate(text: str, max_chars: int = 12000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


_PROMPT = """你是财报分析助手。根据下面美股 10-Q 季报内容，生成详细的中文公司简介。

内容要求(尽量都涵盖，报表里有的信息都要提取):
1. 公司定位: 一句话说清公司做什么
2. 业务构成与营收: 各业务线/产品的营收金额和占比(如"数据中心营收75.2亿美元占92%，其中Hyperscale和AI云各占约50%")，同比增减幅度，毛利率/利润率
3. 地域分布: 各地区营收和占比(美国X亿占X%、中国X亿占X%…)，哪个地区增长最快
4. 核心产品与技术: 主要产品名称、技术平台、在研管线(药企)、重要客户或合作伙伴
5. 本季度亮点: 关键增长驱动力、新产品发布、重大订单、战略转变
6. 资本与财务: 现金流、资本支出、回购、股息等关键财务动作
7. 风险与挑战: 3-5 个最实质的风险，每个用一句话概括(如果没有 Risk Factors 部分，从 MD&A 中提取经营挑战)

严格格式要求:
- 直接从公司名开始写，禁止任何开头语/寒暄/标题
- 写 2-4 段纯中文自然文字，信息密度高，像一篇简短的研究笔记
- 绝对禁止 markdown 格式(** # - 等)
- 风险用序号(1)(2)(3)列出
- 总字数 500-800 字，宁多不少
- 绝对不要问问题、不要说"需要更多信息"——用你拿到的内容直接写，没有的部分跳过"""


def _is_garbage(text: str) -> bool:
    """Detect model outputs that are questions/refusals instead of actual content."""
    markers = ['请问您', '能否', '需要看到', '上传完整', '提供完整',
               '请提供', '请您', '一旦我获得', '无法生成', '信息不足']
    t = text[:500]
    return sum(1 for m in markers if m in t) >= 2


def _clean_summary(text: str) -> str:
    """Strip markdown formatting, preamble, and trailing separators.
    Returns empty string for garbage outputs (model asking questions)."""
    if _is_garbage(text):
        return ""
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'^#{1,3}\s+', '', text, flags=re.M)
    text = re.sub(r'^-{3,}\s*$', '', text, flags=re.M)
    # If model wrote a structured data block then prose, keep only the prose
    # Pattern: "现在让我..." or "以下是..." followed by actual Chinese prose
    for split_marker in [r'现在让我.*?[：:]', r'以下是.*?简介[：:]', r'下面是.*?[：:]']:
        m = re.search(split_marker, text)
        if m and m.start() > 100:
            after = text[m.end():].lstrip()
            if len(after) > 200:
                text = after
                break
    # Strip preamble lines (model chatter before real content)
    lines = text.split('\n')
    skip_to = 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            if skip_to > 0:
                skip_to = i + 1
            continue
        if re.match(r'^(根据|我[看来到发现]|以下是|基于|感谢|让我|好的|公司简介[:：]|I\'ll|Let me|Based on|Here|虽然|但根据)', s):
            skip_to = i + 1
            continue
        if re.match(r'^.{0,60}(季报|10-Q|简介|分析|概览|公司简介|我(应该|会|将))', s) and len(s) < 80:
            skip_to = i + 1
            continue
        # Skip structured data lines (key: value format common in model "notes")
        if re.match(r'^[-·•]\s', s) or re.match(r'^(已有信息|主要产品|风险因素|关键数据)', s):
            skip_to = i + 1
            continue
        break
    if skip_to:
        text = '\n'.join(lines[skip_to:]).lstrip()
    # Strip trailing model self-commentary
    lines2 = text.rstrip().split('\n')
    while lines2:
        last = lines2[-1].strip()
        if not last:
            lines2.pop()
            continue
        if re.match(r'^(字数|注[：:]|以上|总结|备注|说明)', last):
            lines2.pop()
            continue
        break
    return '\n'.join(lines2).strip()


def build_one(ticker: str) -> dict | None:
    """Fetch latest 10-Q for ticker, extract & summarize with Haiku. Returns profile dict or None."""
    from narratives.haiku import _invoke

    cik = _ticker_to_cik(ticker)
    if not cik:
        return None

    filing = _get_latest_10q(cik)
    if not filing:
        return None

    try:
        html = _fetch_filing_html(filing["cik"], filing["accession"], filing["doc"])
    except Exception:
        return None

    sections = _extract_sections(html)
    if not sections["mda"] and not sections["risk"]:
        return None

    user_text = f"Ticker: {ticker}\nFiling date: {filing['filing_date']}\nCompany: {filing['company']}\n"
    user_text += "重要: 以下是已提取的10-Q核心内容(非截断)，直接根据这些内容写简介，不要索要更多信息。\n\n"
    user_text += "=== MD&A (Management's Discussion and Analysis) ===\n"
    user_text += _truncate(sections["mda"], 15000) + "\n\n"
    if sections["risk"]:
        user_text += "=== Risk Factors ===\n"
        user_text += _truncate(sections["risk"], 8000)
    else:
        user_text += "(注意: 本季度10-Q未单独列出Risk Factors，请从MD&A中提取经营风险和挑战)"

    # Override haiku system prompt for this task
    import subprocess, shutil, tempfile, json as _json
    env = dict(os.environ)
    env["MAX_THINKING_TOKENS"] = "0"
    env.pop("ANTHROPIC_API_KEY", None)
    for _k in list(env):
        if (_k.startswith("CLAUDE_CODE") or _k.startswith("CLAUDE_AGENT")
                or _k in ("CLAUDE_EFFORT", "BAGGAGE")):
            env.pop(_k, None)

    claude_bin = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
    cmd = [claude_bin, "-p", "--model", "claude-haiku-4-5", "--output-format", "json",
           "--system-prompt", _PROMPT,
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--setting-sources", "project,local",
           "--no-session-persistence"]
    p = subprocess.run(cmd, input=user_text, capture_output=True, text=True,
                       env=env, cwd=tempfile.gettempdir(), timeout=120)
    if p.returncode != 0:
        return None

    try:
        envelope = _json.loads(p.stdout)
        summary = _clean_summary(envelope.get("result", ""))
    except Exception:
        return None

    if not summary:
        return None

    return {
        "ticker": ticker.upper(),
        "company": filing["company"],
        "filing_date": filing["filing_date"],
        "summary": summary,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def save_one(ticker: str, profile: dict):
    cache = _load_cache()
    cache[ticker.upper()] = profile
    _save_cache(cache)


def build_and_save(ticker: str) -> dict | None:
    profile = build_one(ticker)
    if profile:
        save_one(ticker, profile)
    return profile


def build_all(tickers: list[str] | None = None, max_workers: int = 4,
              skip_existing: bool = True):
    """Batch build profiles for all tickers. Prints progress."""
    if tickers is None:
        from narratives.universe import individual_stocks
        tickers = individual_stocks()

    cache = _load_cache()
    if skip_existing:
        tickers = [t for t in tickers if t not in cache]

    total = len(tickers)
    if not total:
        print("All profiles already cached, nothing to do.")
        return

    print(f"Building {total} company profiles (max_workers={max_workers})...")
    done = 0
    failed = []

    def _do(tk):
        try:
            return tk, build_one(tk)
        except Exception as e:
            return tk, None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_do, t): t for t in tickers}
        for fut in as_completed(futures):
            tk, profile = fut.result()
            done += 1
            if profile:
                save_one(tk, profile)
                print(f"  [{done}/{total}] ✅ {tk}: {profile['company'][:40]}")
            else:
                failed.append(tk)
                print(f"  [{done}/{total}] ❌ {tk}: failed")

    print(f"\nDone: {total - len(failed)}/{total} succeeded, {len(failed)} failed")
    if failed:
        print("Failed:", ", ".join(failed))


if __name__ == "__main__":
    import sys
    if "--all" in sys.argv:
        build_all(max_workers=3, skip_existing="--force" not in sys.argv)
    elif len(sys.argv) > 1:
        tk = sys.argv[1].upper()
        print(f"Building profile for {tk}...")
        p = build_and_save(tk)
        if p:
            print(f"✅ {p['company']}")
            print(p["summary"])
        else:
            print("❌ Failed")
    else:
        print("Usage: python -m narratives.company_profile AAPL")
        print("       python -m narratives.company_profile --all [--force]")
