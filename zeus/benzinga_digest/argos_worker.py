"""轻量本地翻译 worker（en→简中），跑在隔离的 .venv_opus（干净环境、OpenBLAS,
无 OpenMP 重复加载冲突 → 不用 unsafe 开关、不崩）。

用 argostranslate 已下载的 en→zh CTranslate2 NMT 模型 + sentencepiece(直接加载,
**绕过会段错误的 stanza 分句**)。比 qwen 那个 3B LLM 轻太多(~50 条/秒 CPU、基本不转风扇)。
隔离运行:就算崩了也只崩这个子进程,父进程(digest.py)拿到错误 → 退回远程,绝不影响 PWA。

两种模式:
- `argos_worker.py serve`  **常驻**:模型加载一次(~326ms),然后逐行(stdin)收 {"texts":[...]}
  请求、逐行(stdout)出 {"zh":[...]},直到 stdin EOF(父进程退出)才结束。digest.py 维持这个
  长活子进程 → 免每次重载模型(单篇/SEC 逐块从 ~330ms 降到 ~10-50ms)。
- `argos_worker.py`        一次性(旧行为,保留兼容):stdin 读一次 → stdout 出一次 → 退出。
"""
import glob
import json
import os
import sys


def _load():
    """加载 ct2 translator + sentencepiece。返回 (translator, sp);任何失败抛异常。"""
    import ctranslate2
    import sentencepiece as spm
    pkgs = glob.glob(os.path.expanduser("~/.local/share/argos-translate/packages") + "/*en_zh*")
    if not pkgs:
        raise RuntimeError("no argos en_zh package")
    pkg = pkgs[0]
    translator = ctranslate2.Translator(os.path.join(pkg, "model"), device="cpu", intra_threads=4)
    sp = spm.SentencePieceProcessor(os.path.join(pkg, "sentencepiece.model"))
    return translator, sp


def _translate(translator, sp, texts: list) -> list:
    toks = [sp.encode((t or "").replace("\n", " ").strip()[:2000] or "·", out_type=str) for t in texts]
    res = translator.translate_batch(toks, max_batch_size=64)
    return [sp.decode(r.hypotheses[0]).replace("▁", " ").strip() for r in res]


def _emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve() -> None:
    """常驻模式:模型加载一次,逐行服务请求直到 stdin EOF。"""
    try:
        translator, sp = _load()
    except Exception as e:           # 加载失败 → 发一行错误让父进程退回远程,然后退出
        _emit({"zh": None, "err": "load: " + str(e)[:160]})
        return
    while True:
        line = sys.stdin.readline()
        if not line:                 # EOF:父进程关了 stdin → 干净退出
            break
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            _emit({"zh": _translate(translator, sp, data.get("texts") or [])})
        except Exception as e:       # 单条请求出错只回这条的错误,worker 继续服务下一条
            _emit({"zh": None, "err": str(e)[:200]})


def main() -> None:
    """一次性模式(兼容旧调用):stdin 读一次 → stdout 出一次 → 退出。"""
    try:
        translator, sp = _load()
        data = json.loads(sys.stdin.read() or "{}")
        _emit({"zh": _translate(translator, sp, data.get("texts") or [])})
    except Exception as e:
        _emit({"zh": None, "err": str(e)[:200]})


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve()
    else:
        main()
