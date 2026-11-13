"""把 heatmap + TA screener 的 universe 同步到唯一权威源 core_universe.txt。

权威源 = `data/options_vol/core_universe.txt`(纯 ticker,每行一个,~399 票:
gold+silver 期权波动池 − ADR + SPCX)。本模块把这份名单**翻译**成两个消费方
各自需要的磁盘格式,原样保持它们的解析约定,不动任何 *.py:

  · heatmap   → `data/heatmap_universe.txt`(单行逗号分隔,内联 `###分类名`;
                名字含 "ETF" 的分类标记其票为 ETF;token 形如 EXCHANGE:TICKER)。
                做法:逐分类只留仍在 core 里的 token、删空分类(**保留 ###…ETF
                分类**让 ETF 标记存活),末尾追加 `###Options 核心(自动同步)`
                收纳 core 里尚未出现的票。sector 仍由 yfinance(metadata.json)
                回填,故新票(SPCX)只要出现在列表里即可。

  · TA        → `data/ta_screener_universe.txt`(单行逗号分隔 EXCHANGE:TICKER,
                无分类)。全量 core 重写。
                `data/ta_screener_pool.txt` 再由 build_pool 从新 universe 重筛
                (过滤掉「不会动/太便宜/太贵/没催化」的票);build_pool 报错或
                跑太久 → 退化成「池=全 core」(TA 扫全池)。

两个解析器都对 symbol 做 `.upper().replace(".","-")`(BRK.B→BRK-B),所以 core
里的原始写法(BRK.B)直接拼 EXCHANGE 输出即可,解析端自会归一。

跑法(repo 根):
    PYTHONPATH=. python3 -c "from options_analyzer.options_vol import sync_consumers as s; print(s.sync_all())"
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

# options_analyzer/options_vol/sync_consumers.py → 仓库根 = parent×3
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA = _REPO_ROOT / "data"

_CORE_DEFAULT = _DATA / "options_vol" / "core_universe.txt"
_HEATMAP_TXT = _DATA / "heatmap_universe.txt"
_TA_UNIVERSE_TXT = _DATA / "ta_screener_universe.txt"
_TA_POOL_TXT = _DATA / "ta_screener_pool.txt"

# EXCHANGE:TICKER 映射的来源(都是这个格式),优先级靠前者先填、后者不覆盖。
_EXCH_SOURCES = [
    _HEATMAP_TXT,
    _TA_UNIVERSE_TXT,
    _DATA / "options_vol" / "sources" / "watchlist.txt",
]

# 末尾自动同步分类名(名字不含 "ETF",故这些票按普通股处理)。
_SYNC_SECTION = "Options 核心(自动同步)"

# build_pool 跑超过这个秒数就放弃、走 fallback(下载历史可能很慢)。
_POOL_BUILD_TIMEOUT = 200


def _norm(sym: str) -> str:
    """与两个消费端解析器一致的归一:取 ':' 后段、大写、`.`→`-`。"""
    return sym.split(":")[-1].strip().upper().replace(".", "-")


def _load_core(core_path: Path) -> list[str]:
    """读 core(每行一个 ticker,保原始写法 BRK.B)→ 有序去重列表。"""
    out, seen = [], set()
    for ln in core_path.read_text(encoding="utf-8").splitlines():
        t = ln.strip()
        if not t or t.startswith("#"):
            continue
        key = _norm(t)
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def _build_exch_map() -> dict[str, str]:
    """{归一ticker: "EXCH:TICKER"};扫所有来源,先到先得(后续不覆盖)。

    value 保留来源里的原始写法(交易所 + 原 ticker,如 NYSE:BRK.B),让
    消费端解析器自己 `.replace('.','-')`。
    """
    exch: dict[str, str] = {}
    for src in _EXCH_SOURCES:
        if not src.exists():
            continue
        raw = src.read_text(encoding="utf-8")
        for tok in raw.replace("\n", ",").split(","):
            tok = tok.strip()
            if not tok or tok.startswith("###"):
                continue
            if ":" not in tok:
                continue
            key = _norm(tok)
            if key and key not in exch:
                exch[key] = tok  # 原始 "EXCH:TICKER"
    return exch


def _exch_token(orig_ticker: str, exch: dict[str, str]) -> str:
    """core 里的原始 ticker → 输出用的 token。

    有交易所映射 → 用映射的原始 "EXCH:TICKER";SPCX 兜底 NASDAQ:SPCX;
    其余未知交易所 → 裸 ticker(解析器容忍)。
    """
    key = _norm(orig_ticker)
    if key in exch:
        return exch[key]
    if key == "SPCX":
        return "NASDAQ:SPCX"
    return orig_ticker  # 裸 ticker(保原始写法,如 BRK.B)


def _parse_heatmap_sections(raw: str) -> list[tuple[str, list[str]]]:
    """heatmap 单行 → 有序 [(section_name, [token,…]), …]。

    section_name 是 `###` 后的原文(不含 ###);文件开头若有 section 之前的
    裸 token(本文件无),归到名为 "" 的隐式分类。token 保原始写法。
    """
    sections: list[tuple[str, list[str]]] = []
    cur_name = ""
    cur_toks: list[str] = []
    started = False
    for tok in raw.replace("\n", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.startswith("###"):
            if started:
                sections.append((cur_name, cur_toks))
            cur_name = tok[3:].strip()
            cur_toks = []
            started = True
        else:
            if not started:
                # 开头裸 token:开一个匿名分类
                started = True
                cur_name = ""
            cur_toks.append(tok)
    if started:
        sections.append((cur_name, cur_toks))
    return sections


def _serialize_heatmap_sections(sections: list[tuple[str, list[str]]]) -> str:
    """[(name,[toks]),…] → 原磁盘形态(单行逗号分隔,内联 ###,无尾换行)。"""
    parts: list[str] = []
    for name, toks in sections:
        parts.append("###" + name)
        parts.extend(toks)
    return ",".join(parts)


def _regen_heatmap(core: list[str], exch: dict[str, str]) -> tuple[int, list[str], int]:
    """重写 heatmap_universe.txt(先备份 .bak)。

    返回 (写入后去重票数, 新追加的 token 列表, 被删的 token 数)。
    """
    raw = _HEATMAP_TXT.read_text(encoding="utf-8")
    _HEATMAP_TXT.with_suffix(_HEATMAP_TXT.suffix + ".bak").write_text(raw, encoding="utf-8")

    core_keys = {_norm(t) for t in core}
    sections = _parse_heatmap_sections(raw)

    kept_keys: set[str] = set()
    dropped = 0
    new_sections: list[tuple[str, list[str]]] = []
    for name, toks in sections:
        keep_toks = []
        for tk in toks:
            if _norm(tk) in core_keys:
                keep_toks.append(tk)
                kept_keys.add(_norm(tk))
            else:
                dropped += 1
        # 删空分类(包括空掉的 ETF 分类没意义——但凡 ETF 分类里还有 core 票就留;
        # 全空才删,ETF 标记靠仍在 core 的那些 ETF 票存活)。
        if keep_toks:
            new_sections.append((name, keep_toks))

    # core 里尚未出现的票 → 追加到自动同步分类
    added = [_exch_token(t, exch) for t in core if _norm(t) not in kept_keys]
    if added:
        new_sections.append((_SYNC_SECTION, added))

    out = _serialize_heatmap_sections(new_sections)
    _HEATMAP_TXT.write_text(out, encoding="utf-8")

    # 去重票数(按归一 key)
    all_keys = {_norm(tk) for _n, toks in new_sections for tk in toks}
    return len(all_keys), added, dropped


def _regen_ta_universe(core: list[str], exch: dict[str, str]) -> int:
    """重写 ta_screener_universe.txt(先备份 .bak):全量 core,单行逗号分隔
    EXCH:TICKER,无尾换行(对齐现有文件格式)。返回票数。"""
    raw = _TA_UNIVERSE_TXT.read_text(encoding="utf-8")
    _TA_UNIVERSE_TXT.with_suffix(_TA_UNIVERSE_TXT.suffix + ".bak").write_text(raw, encoding="utf-8")

    toks = [_exch_token(t, exch) for t in core]
    _TA_UNIVERSE_TXT.write_text(",".join(toks), encoding="utf-8")
    return len(toks)


def _rebuild_ta_pool(core: list[str]) -> tuple[int, str]:
    """重建 ta_screener_pool.txt。

    先试 `ta_screener.build_pool`(从新 universe 重筛,合格票写池);报错/超时
    → fallback:池 = 全 core(裸 ticker,逗号分隔),TA 扫全池。
    返回 (池票数, "build_pool"|"fallback")。

    用子进程跑 build_pool(隔离 + 可超时;它 import data 时裸 `import data`
    需 ta_screener/ 在 sys.path,故 cwd=repo 根 + PYTHONPATH=.)。
    """
    # 先快照备份(无论走哪条路都保一份原池)
    if _TA_POOL_TXT.exists():
        old = _TA_POOL_TXT.read_text(encoding="utf-8")
        _TA_POOL_TXT.with_suffix(_TA_POOL_TXT.suffix + ".bak").write_text(old, encoding="utf-8")

    code = (
        "import sys; sys.path.insert(0, 'ta_screener'); "
        "from ta_screener import build_pool as bp; "
        "bp.build(log=lambda *a, **k: None)"
    )
    env_pythonpath = str(_REPO_ROOT)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(_REPO_ROOT),
            env={**_os_environ_with_pp(env_pythonpath)},
            capture_output=True,
            text=True,
            timeout=_POOL_BUILD_TIMEOUT,
        )
        ok = proc.returncode == 0
    except subprocess.TimeoutExpired:
        ok = False
    except Exception:
        ok = False

    if ok and _TA_POOL_TXT.exists():
        # build_pool 已写池;数一下票数
        raw = _TA_POOL_TXT.read_text(encoding="utf-8")
        n = len([t for t in raw.replace("\n", ",").split(",") if t.strip()])
        if n > 0:
            return n, "build_pool"

    # fallback:池 = 全 core(裸 ticker)
    pool_toks = [t for t in core]
    _TA_POOL_TXT.write_text(",".join(pool_toks), encoding="utf-8")
    return len(pool_toks), "fallback"


def _os_environ_with_pp(pythonpath: str) -> dict:
    import os

    env = dict(os.environ)
    prev = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = pythonpath + (os.pathsep + prev if prev else "")
    return env


def sync_all(core_path: str | Path | None = None) -> dict:
    """把 heatmap + TA 的 universe 同步到 core。

    core_path 缺省 = data/options_vol/core_universe.txt。返回计数 dict:
    {"heatmap_n", "ta_universe_n", "ta_pool_n", "added"(list), "dropped"(int),
     "pool_mode": "build_pool"|"fallback"}。
    """
    core_p = Path(core_path) if core_path else _CORE_DEFAULT
    core = _load_core(core_p)
    exch = _build_exch_map()

    heatmap_n, added, dropped = _regen_heatmap(core, exch)
    ta_universe_n = _regen_ta_universe(core, exch)
    ta_pool_n, pool_mode = _rebuild_ta_pool(core)

    return {
        "heatmap_n": heatmap_n,
        "ta_universe_n": ta_universe_n,
        "ta_pool_n": ta_pool_n,
        "added": added,
        "dropped": dropped,
        "pool_mode": pool_mode,
    }


if __name__ == "__main__":
    import json

    t0 = time.time()
    res = sync_all()
    res["_elapsed_s"] = round(time.time() - t0, 1)
    print(json.dumps(res, ensure_ascii=False, indent=2))
