#!/usr/bin/env python
"""全历史正文预翻(英→简中)，写入 .<date>.tr.json 的 'b' 字段({id:{b:[中文段...]}})。

多后端「穿插」策略(作者要求)：
  1) Google POST 批量(免费, 首选)。一旦 429/失败 → Google 进入冷却(默认90s)，
     这段时间交给 OpenAI / qwen，冷却完 Google 自动恢复 → 天然穿插、避免持续限流。
  2) OpenAI 便宜模型(并发, 快)，累计花费有硬上限(BENZINGA_OPENAI_BUDGET,默认 $20)。
     需 key：环境变量 OPENAI_API_KEY 或文件 benzinga_digest/openai_key.txt。
  3) 本地 qwen 兜底(免费, 慢)，永远可用。
预算花完/无 key → 仅 Google+qwen。不碰主 digest、不需要 .run.lock(只读 .json 写 .tr.json)。
独立进度 _bodies_progress.json(含累计 openai 花费) + 日志 _bodies.log，可断点续传。

    BENZINGA_OPENAI_MODEL=gpt-4o-mini python backfill_bodies.py
"""
from __future__ import annotations

import os
import sys
import json
import time
import datetime as dt
import traceback
from pathlib import Path
import threading
from queue import Queue, Empty

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))
from benzinga_mcp.digest import openai_translate_lines, qwen_translate_lines, tr_update
from benzinga_mcp.digest_store import list_days as _ds_list_days, load_day as _ds_load_day  # 认 .json/.json.zst

DIG = HERE / "digests"
_SUF = "_opus" if os.environ.get("BENZINGA_ENGINE", "openai") == "opus" else ""  # 双引擎独立进度/日志
LOG = DIG / f"_bodies{_SUF}.log"
PROG = DIG / f"_bodies{_SUF}_progress.json"
STARTED = DIG / f"_bodies{_SUF}_started.txt"
# 前端控制(仅 nano/openai)：控制文件 + pidfile，供 PWA 暂停/继续/停止 + 识别 nano 进程
CONTROL = DIG / "_bodies_control.json"   # {"nano": "run"|"pause"|"stop"}
NANO_PID = DIG / "_bodies_nano.pid"

# ── OpenAI 配置：gpt-4.1-mini + gpt-4.1-nano 两模型并行(轮询)，累计花费共享一个上限 ──
# 价格($/百万token)：4.1-nano $0.10/$0.40, 4.1-mini $0.40/$1.60。
OPENAI_MODELS = [
    {"model": "gpt-4.1-nano", "pin": 0.10, "pout": 0.40},
    {"model": "gpt-4.1-mini", "pin": 0.40, "pout": 1.60},
]  # nano + mini 轮询(作者要求 2026-06-04)：两模型都健康, 共享 $ 上限
OPENAI_BUDGET = float(os.environ.get("BENZINGA_OPENAI_BUDGET", "20"))
# 双车道并行：OpenAI 多 worker(快,大批量) + 本地 qwen 多 worker(免费,小批量), 同一队列取活
N_OPENAI = int(os.environ.get("BENZINGA_OPENAI_WORKERS", "12"))  # 小批(4000字符≈3000tok)×12 ≈130K TPM, 稳在 200K 下, 充分利用额度
N_QWEN = int(os.environ.get("BENZINGA_QWEN_WORKERS", "0"))       # 作者要求: 不用 qwen(它是瓶颈)
OPENAI_BATCH_CHARS = 4000    # 每批 ~30 段: 小批→nano 对齐更可靠(少 misalign→少掉 qwen) + 低 TPM
CHUNK_CHARS = OPENAI_BATCH_CHARS   # _chunks() 的切块预算(原代码引用了却从未定义)
# 正文只翻这些年份(逗号分隔); 其余天只翻标题。短文(几句话的评级/目标价/财报/并购/IPO)不翻正文。
BODIES_YEARS = set(y.strip() for y in os.environ.get("BENZINGA_BODIES_YEARS", "2026").split(",") if y.strip())
BODY_MIN_CHARS = int(os.environ.get("BENZINGA_BODY_MIN_CHARS", "500"))  # 正文总字符 < 此值=短文, 跳过(留按需翻)
# 只翻标题模式: 跳过所有正文(正文改为点开文章时按需 Google 翻, /api/digest/translate_article)。
# 标题轻、几乎不触发 OpenAI 并发 SSL 挂起 → 快很多。作者要求(2026-06-04)。
TITLES_ONLY = os.environ.get("BENZINGA_TITLES_ONLY", "0") == "1"
QWEN_BATCH_N = 10            # qwen 每批段数(3b 编号对齐可靠上限, 小批)
QWEN_BATCH_CHARS = 2500
os.environ.setdefault("OLLAMA_NUM_PARALLEL", str(N_QWEN))  # 让本地 ollama 并发服务 qwen

NEWEST_FIRST = os.environ.get("BENZINGA_BODIES_ORDER", "new") != "old"


def _read_key() -> str:
    k = os.environ.get("OPENAI_API_KEY", "").strip()
    if k:
        return k
    f = HERE / "openai_key.txt"
    try:
        return f.read_text().strip()
    except Exception:
        return ""


KEY = _read_key()

import itertools
_model_cycle = itertools.cycle(OPENAI_MODELS)   # 两模型轮询
_state = {"cooldown_until": 0.0, "openai_cost": 0.0, "g": 0, "o": 0, "q": 0,
          "last_ok": 0.0, "fb": 0}   # last_ok=最近一次成功 OpenAI 调用; fb=opus 兜底次数(wedge 监测)
import threading
_lock = threading.Lock()
_google_lock = threading.Lock()   # Google 全局串行(同一时刻只 1 个请求)→ 不自我 429
_opus_lock = threading.Lock()     # 本地 ct2 兜底串行(单实例 Translator, 避免多 worker 抢)


def log(m: str) -> None:
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {m}"
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_prog() -> dict:
    try:
        p = json.loads(PROG.read_text())
        _state["openai_cost"] = p.get("_openai_cost", 0.0)
        return p
    except Exception:
        return {}


def save_prog(p: dict) -> None:
    p["_openai_cost"] = round(_state["openai_cost"], 4)
    try:
        tmp = PROG.with_suffix(".tmp")
        tmp.write_text(json.dumps(p, ensure_ascii=False))
        os.replace(tmp, PROG)
    except Exception:
        pass


def _control_state() -> str:
    """读前端控制状态(仅 nano)。缺省 run。"""
    try:
        return str(json.loads(CONTROL.read_text()).get("nano", "run"))
    except Exception:
        return "run"


def all_dates() -> list:
    """只处理 BODIES_YEARS(2025-2026) 的天：翻标题 + 长文正文。其余年份完全不碰(靠点击按需翻)。"""
    out = set()
    for n in _ds_list_days():   # 认 .json 与 .json.zst(compact 后不再依赖 .md)
        if BODIES_YEARS and n[:4] not in BODIES_YEARS:
            continue
        out.add(n)
    return sorted(out, reverse=NEWEST_FIRST)


def _chunks(items):
    """把 (aid,idx,text) 列表按字符预算切块。"""
    cur, clen = [], 0
    for it in items:
        t = it[2] or ""
        if cur and clen + len(t) > CHUNK_CHARS:
            yield cur; cur, clen = [], 0
        cur.append(it); clen += len(t) + 1
    if cur:
        yield cur


def _openai_chunk(texts: list):
    """OpenAI 翻一批；超预算→None(交 qwen)。对齐失败→二分拆开再试 OpenAI(避免掉到 qwen/ollama),
    直到单条仍失败才 None。"""
    if not (KEY and _state["openai_cost"] < OPENAI_BUDGET):
        return None
    if not texts:
        return []
    mc = next(_model_cycle)
    r, pt, ct = openai_translate_lines(texts, mc["model"], KEY)
    if r is not None:
        with _lock:
            _state["openai_cost"] += pt / 1e6 * mc["pin"] + ct / 1e6 * mc["pout"]
            _state["o"] += len(texts)
            _state["last_ok"] = time.time()   # wedge 监测: 最近一次成功 OpenAI
        return r
    # 仅"拿到响应但对齐失败"(pt>0) 才二分细化；超时/网络失败(pt==0) 直接放弃 →
    # 交上层退回本地 opus。绝不在超时上二分(那会把一次抖动指数级放大成数小时死循环)。
    if pt > 0 and len(texts) > 1:
        mid = len(texts) // 2
        a = _openai_chunk(texts[:mid])
        b = _openai_chunk(texts[mid:])
        if a is not None and b is not None:
            return a + b
    return None


ENGINE = os.environ.get("BENZINGA_ENGINE", "openai")   # openai | opus(本地原生 ct2)
OPUS_PKG = os.environ.get("OPUS_PKG",
    os.path.expanduser("~/.local/share/argos-translate/packages/translate-en_zh-1_9"))
_opus = {"tr": None, "sp": None}


def _opus_init():
    if _opus["tr"]:
        return
    import ctranslate2
    import sentencepiece as spm
    _opus["sp"] = spm.SentencePieceProcessor(model_file=OPUS_PKG + "/sentencepiece.model")
    _opus["tr"] = ctranslate2.Translator(OPUS_PKG + "/model", device="cpu",
                                         inter_threads=4, intra_threads=3)


def _opus_translate(texts: list) -> list:
    """本地原生 ct2(opus-mt en→zh) 批量翻译。空串原样返回。
    _opus_lock 串行化：单实例 Translator 自带 inter/intra 线程，多 worker 兜底时不抢。"""
    with _opus_lock:
        _opus_init()
        sp, tr = _opus["sp"], _opus["tr"]
        idx = [i for i, t in enumerate(texts) if t and t.strip()]
        toks = [sp.encode(texts[i], out_type=str) for i in idx]
        out = list(texts)
        if toks:
            res = tr.translate_batch(toks, max_batch_size=128, beam_size=1)
            for k, r in zip(idx, res):
                out[k] = sp.decode(r.hypotheses[0]).replace("▁", " ").strip()  # 清理漏出的 ▁
    return out


def _qwen_chunk(texts: list) -> list:
    """qwen 批量翻一小批(编号对齐, 不齐逐条兜底)。"""
    r = qwen_translate_lines(texts)
    with _lock:
        _state["q"] += len(texts)
    return r


def do_day(date: str) -> int:
    """双车道并行翻一天：标题+正文打平进一个队列, OpenAI 车道(大批量,快) 与
    qwen 车道(小批量,免费) 同时取活, 预算到顶后 OpenAI 车道也转 qwen。"""
    metas = _ds_load_day(date)   # 认 .json 与 .json.zst(compact 后)
    if not metas:
        return (0, 0)
    tp = DIG / date[:7] / f".{date}.tr.json"
    try:
        cache = json.loads(tp.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    # TITLES_ONLY → 一律跳过正文(按需 Google 翻); 否则按 BODIES_YEARS 决定哪些年翻正文
    do_bodies = (not TITLES_ONLY) and ((not BODIES_YEARS) or date[:4] in BODIES_YEARS)
    flat = []          # (ref, text)；ref=('t',aid) 标题 或 ('b',aid,idx) 正文
    order = {}         # aid -> 正文段数
    b_updates = {}
    for m in metas:
        aid = str(m.get("id"))
        ent = cache.get(aid, {})
        if m.get("title") and not ent.get("t"):
            flat.append((("t", aid), m["title"]))
        if do_bodies and not ent.get("b"):
            paras = [(p.get("cn") or p.get("en")) for p in (m.get("paras") or [])
                     if (p.get("cn") or p.get("en"))]
            total = sum(len(p) for p in paras)
            if not paras:
                b_updates[aid] = []
            elif total < BODY_MIN_CHARS:
                pass                 # 短文(几句话)跳过正文, 留按需翻; 标题仍翻
            else:
                order[aid] = len(paras)
                for i, t in enumerate(paras):
                    flat.append((("b", aid, i), t))
    if not flat and not b_updates:
        return (0, 0)

    results: dict = {}
    if ENGINE == "opus":
        # 本地原生 ct2(opus-mt) 一次性批量翻这一天的全部(标题+长文段)
        cn = _opus_translate([t for _, t in flat])
        for (ref, _), v in zip(flat, cn):
            results[ref] = v
        with _lock:
            _state["q"] += len(flat)         # 复用 Q 计数代表 opus
        return _finish_day(date, metas, order, b_updates, results, cache)

    cq: Queue = Queue()
    for item in flat:
        cq.put(item)

    def pull(maxn, maxchars):
        batch, cl = [], 0
        while len(batch) < maxn:
            try:
                it = cq.get_nowait()
            except Empty:
                break
            batch.append(it); cl += len(it[1] or "")
            if cl >= maxchars:
                break
        return batch

    def openai_lane():
        while True:
            batch = pull(400, OPENAI_BATCH_CHARS)
            if not batch:
                return
            texts = [t for _, t in batch]
            cn = _openai_chunk(texts)
            if cn is None:                       # OpenAI 超时/失败/预算尽 → 本地 opus 兜底(永不停)
                with _lock:
                    _state["fb"] = _state.get("fb", 0) + 1
                    n = _state["fb"]
                if n <= 3 or n % 50 == 0:        # 节流日志: 再也不会静默冻住
                    log(f"⚠ OpenAI 失败,转本地 opus 兜底(累计 {n} 批) — 翻译继续不中断")
                cn = _opus_translate(texts)
                with _lock:
                    _state["q"] += len(texts)    # 复用 Q 计数代表本地引擎
            for (ref, _), v in zip(batch, cn):
                results[ref] = v

    def qwen_lane():
        while True:
            batch = pull(QWEN_BATCH_N, QWEN_BATCH_CHARS)
            if not batch:
                return
            cn = _qwen_chunk([t for _, t in batch])
            for (ref, _), v in zip(batch, cn):
                results[ref] = v

    threads = [threading.Thread(target=openai_lane) for _ in range(N_OPENAI)] + \
              [threading.Thread(target=qwen_lane) for _ in range(N_QWEN)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return _finish_day(date, metas, order, b_updates, results, cache)


def _finish_day(date, metas, order, b_updates, results, cache):
    """组装 results(标题/正文译文) → 写 .tr.json(flock)。返回 (标题数, 正文篇数)。"""
    t_updates = {}
    body_arr = {aid: [None] * n for aid, n in order.items()}
    for ref, v in results.items():
        if ref[0] == "t":
            t_updates[ref[1]] = v
        else:
            body_arr[ref[1]][ref[2]] = v
    for aid, arr in body_arr.items():
        b_updates[aid] = [x if x is not None else "" for x in arr]

    def _mut(c):
        for aid, v in t_updates.items():
            c.setdefault(aid, {})["t"] = v
        for aid, arr in b_updates.items():
            c.setdefault(aid, {})["b"] = arr
    tr_update(date, _mut)
    return (len(t_updates), len(order))


def _wedge_watchdog(prog: dict) -> None:
    """逃逸 OpenAI 连接 wedge。实测:并发 HTTPS 到 api.openai.com 会间歇性在 SSL read 上
    无限挂起(外部突发, 时变);_urlopen_hard 硬超时遗弃的线程持有卡死连接, 累积后**整个
    长跑进程的新请求也挂**(全新进程则正常)——无法自愈。这里检测"持续无成功 OpenAI 调用
    + 仍在 opus 兜底"即判 wedge, 歇 30s(让外部突发过去)后 os.execv 换全新进程拿干净 SSL 栈。
    进度已落盘, 重启后跳过已完成天续翻; 纯本地 opus 兜底期间翻译不中断。"""
    last_fb = 0
    while True:
        time.sleep(20)
        if ENGINE != "openai" or _control_state() != "run":
            _state["last_ok"] = time.time()   # 暂停/停止: 不算 wedge
            last_fb = _state.get("fb", 0)
            continue
        since = time.time() - _state["last_ok"]
        fb = _state.get("fb", 0)
        if since > 120 and fb > last_fb:      # 120s 无成功 + 这期间在兜底 = 连接 wedge
            log(f"⚠ {int(since)}s 无成功 OpenAI 调用(连接 wedge)→ 歇 30s 后重启拿全新连接")
            try:
                save_prog(prog)
            except Exception:
                pass
            time.sleep(30)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        last_fb = fb


def main() -> int:
    if ENGINE == "opus":
        eng = "本地原生 ct2(opus-mt)"
    else:
        eng = f"OpenAI×{N_OPENAI}({'+'.join(m['model'] for m in OPENAI_MODELS)},上限${OPENAI_BUDGET})"
    _scope = ("⚡只标题模式(正文按需 Google 翻)" if TITLES_ONLY
              else f"正文限【{','.join(sorted(BODIES_YEARS)) or '全部'}】年(只翻≥{BODY_MIN_CHARS}字符长文)")
    log(f"=== 预翻启动[{ENGINE}]: {eng}; {_scope}; 其余只标题; {'新→旧' if NEWEST_FIRST else '旧→新'} ===")
    prog = load_prog()
    dates = all_dates()
    log(f"共 {len(dates)} 天, 已用 OpenAI ${_state['openai_cost']:.2f}")
    if ENGINE == "openai":
        try:
            NANO_PID.write_text(str(os.getpid()))
        except Exception:
            pass
        _state["last_ok"] = time.time()   # 基准: 防 watchdog 启动即误判
        threading.Thread(target=_wedge_watchdog, args=(prog,), daemon=True).start()
    done = err = 0
    t0 = time.time()
    for d in dates:
        if ENGINE == "openai":
            st = _control_state()
            if st == "pause":
                log("⏸ 已暂停(前端控制)，等待继续…")
                while _control_state() == "pause":
                    time.sleep(2)
                if _control_state() == "stop":
                    log("⏹ 收到停止指令，退出"); break
                log("▶ 继续")
            elif st == "stop":
                log("⏹ 收到停止指令，退出"); break
        if prog.get(d) is True:
            continue
        try:
            nt, nb = do_day(d)
            prog[d] = True
            save_prog(prog)
            done += 1
            if nt or nb:
                log(f"{d}: 标题{nt} 正文{nb}篇 | O{_state['o']}/Q{_state['q']}段 | OpenAI ${_state['openai_cost']:.2f}")
        except Exception as e:
            err += 1
            log(f"{d}: ❌ {e}\n{traceback.format_exc()[:200]}")
        if done and done % 20 == 0:
            el = time.time() - t0; rate = el / done
            left = sum(1 for x in dates if prog.get(x) is not True)
            log(f"[进度] {done}/{len(dates)} 天, 失败 {err}, ~{rate:.1f}s/天, 剩 ~{left} 天 (~{left*rate/3600:.1f}h), "
                f"OpenAI ${_state['openai_cost']:.2f}/{OPENAI_BUDGET:.0f}")
    log(f"=== 正文预翻完成: {done} 天, 失败 {err}, OpenAI 花费 ${_state['openai_cost']:.2f} ===")
    if ENGINE == "openai":
        try:
            NANO_PID.unlink()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
