"""开单前体检 CLI —— 唯一的展示层,故意写得直白/扎眼,别让人假装没看见 fail。

用法:
  python -m pre_trade_checkup.cli --ticker SPY --direction bull --dte 40
  echo "理由(>=20字): ..." | python -m pre_trade_checkup.cli --ticker SPY --direction bull --override -

退出码:0=可以开单(或 override 已记录),1=别开单且未 override。
"""
from __future__ import annotations

import argparse
import sys

from . import config, engine

_BAR = "━" * 51
_LAYER_TITLE = {1: "长期趋势", 2: "EMA触及", 3: "IV / 财报", 4: "宏观 / benzinga"}


def _status(ok: bool) -> str:
    return "✅ PASS" if ok else "❌ FAIL"


def _render_layer(n: int, l: dict) -> None:
    header = f"Layer {n} ({_LAYER_TITLE[n]})"
    print(f"{header.ljust(22)} {_status(l['pass'])}")
    if l.get("lines"):
        # 固定自然顺序展示(每行自带状态图标),不按 pass/fail 分桶打乱阅读顺序
        for r in l["lines"]:
            print(f"  · {r}")
    else:
        for r in l.get("reasons_pass", []):
            print(f"  · {r}")
        for r in l.get("reasons_fail", []):
            print(f"  · {r}")
    if n == 4:
        for w in l.get("warnings", []):
            print(f"  · ⚠️ {w}")
        hot = l.get("hot_topics") or []
        if hot:
            print("  · 🔥 热点话题:")
            for h in hot:
                thr = f"P95={h['p95']}" if h["meets_p95_highlift"] else f"P90={h['p90']}"
                tail = "  ← 高危,单独否决" if h["meets_p95_highlift"] else ""
                print(f"    · {h['topic']}: {h['count']} 次 ({thr}, lift {h['lift']:.2f}){tail}")
    print()


def _suggestions(layers: dict, failed: list) -> str:
    frags = []
    if 1 in failed:
        frags.append("等票和大盘重新站回同方向的 SMA200 再看")
    if 2 in failed:
        frags.append("等下次真正触及 EMA(20/50/100/200 之一)的回踩/回抽再开")
    if 3 in failed:
        l3 = layers[3]
        if l3.get("ivr") is not None and l3["ivr"] > config.IVR_MAX:
            frags.append("等 IV 回落到中性区间再买借方价差")
        if l3.get("days_to_earnings") is not None:
            frags.append("换一个不撞财报窗口的到期日")
    if 4 in failed:
        l4 = layers[4]
        bits = []
        if l4.get("vix") is not None and l4["vix"] > config.VIX_FAIL:
            bits.append("VIX 回落到 20 以下")
        if l4.get("spy_5d") is not None and l4["spy_5d"] < config.SPY_5D_FAIL_PCT:
            bits.append("大盘企稳")
        if l4.get("saturation_score", 0) >= config.SATURATION_FAIL or l4.get("hot_topics"):
            bits.append("benzinga 话题降温")
        frags.append("等" + "/".join(bits or ["宏观转松"]) + "后再看")
    return "；".join(frags) if frags else "条件都满足,可以按计划执行"


def render(result: dict) -> None:
    asof_note = f" (数据截至 {result['as_of']})" if result.get("as_of") else ""
    print(_BAR)
    print(f"📛 开单前体检 — {result['ticker']} {result['direction'].upper()} {result['dte']}DTE{asof_note}")
    print(_BAR)
    print()

    for n in (1, 2, 3, 4):
        _render_layer(n, result["layers"][n])

    failed = result["failed_layers"]
    print(_BAR)
    if result["pass"]:
        print("✅ verdict: 可以开单")
        print("   4 层全过,按计划执行(仓位/止盈纪律照旧:≤3-5% NLV、90% TP、不止损)")
    else:
        names = "、".join(f"Layer {n} {_LAYER_TITLE[n]}" for n in failed)
        print("🛑 verdict: 别开单")
        print(f"   4 层里 {len(failed)} 层未通过 ({names})")
        print(f"   建议: {_suggestions(result['layers'], failed)}")
    print(_BAR)

    if not result["pass"]:
        print()
        print("如果你一定要开:")
        print(f'  echo "理由(>=20字): 我认为..." | python -m pre_trade_checkup.cli '
              f"--ticker {result['ticker']} --direction {result['direction']} "
              f"--dte {result['dte']} --override -")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m pre_trade_checkup.cli", description="开单前体检 —— 四层过滤器")
    p.add_argument("--ticker", required=True, help="标的,如 SPY")
    p.add_argument("--direction", required=True, choices=["bull", "bear"], help="bull=看涨价差 bear=看跌价差")
    p.add_argument("--dte", type=int, default=config.DEFAULT_DTE, help=f"到期天数,默认 {config.DEFAULT_DTE}")
    p.add_argument("--today", default=None, help="YYYY-MM-DD,默认用本地库最新一根收盘当'今天'")
    p.add_argument("--override", default=None,
                   help="记录'我知道有 fail 但还是要开'的理由(>=20字);传 '-' 从 stdin 读")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    result = engine.run_checkup(args.ticker, args.direction, args.dte, args.today)
    render(result)

    if args.override is None:
        return 0 if result["pass"] else 1

    reason = sys.stdin.read().strip() if args.override == "-" else args.override
    print()
    try:
        entry = engine.log_override(args.ticker, args.direction, args.dte, reason)
    except ValueError as e:
        print(f"❌ override 未记录: {e}")
        return 1
    print(f"⚠️  override 已记录 —— {entry['ts']}")
    print(f"   理由: {entry['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
