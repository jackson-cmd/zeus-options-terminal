"""技术面支撑读数 —— 纯函数, 输入是价格序列, 不碰磁盘/网络。

设计原则: 市场一跌, 人很容易联想到 2000 年、2008 年那样的熊市, 但多数时候只是一次
10% 左右的回撤。对抗这种灾难化最有效的不是安慰, 是**具体位置**:

    离 52 周高点还有多远?  在 200 日均线上方还是下方?  跌幅够得上「回调」还是只是波动?

SPY 离高点 2.8%、在 MA200 上方 5.7% —— 这跟 2008 差着数量级, 数字比感受可靠。

⚠️ 数据新鲜度必须检查: daily_ohlc 只对 heatmap universe 的票每日更新, 别的
   ETF 可能停在几周前。陈旧数据算出来的「技术面支撑」是假的, 宁可标注缺失。
"""
from __future__ import annotations

import datetime as _dt

# 回撤分级(标普口径的常用划分)
def drawdown_bucket(dd_pct: float) -> str:
    """dd_pct 为负数(距高点)。→ 幅度档位。"""
    d = abs(dd_pct)
    if d < 3:
        return "高位波动"
    if d < 5:
        return "浅回调"
    if d < 10:
        return "常规回调"
    if d < 20:
        return "深度回调"
    return "熊市级别"


def read(closes, today=None, stale_days: int = 5, last_date=None) -> dict | None:
    """一条价格序列 → 技术面读数。

    closes    收盘价序列(按时间升序)
    last_date 序列最后一根的日期(用于新鲜度判断); None 表示不检查
    返回 None = 数据不足或过于陈旧(宁可没有, 不要假的)。
    """
    xs = [float(c) for c in (closes or []) if isinstance(c, (int, float))]
    if len(xs) < 60:
        return None

    stale = False
    if last_date is not None and today is not None:
        try:
            ld = _dt.date.fromisoformat(str(last_date)[:10])
            td = _dt.date.fromisoformat(str(today)[:10])
            if (td - ld).days > stale_days:
                stale = True
        except Exception:
            pass
    if stale:
        return None

    last = xs[-1]
    ma50 = sum(xs[-50:]) / 50
    ma200 = (sum(xs[-200:]) / 200) if len(xs) >= 200 else None
    hi252 = max(xs[-252:]) if len(xs) >= 252 else max(xs)
    dd = (last / hi252 - 1.0) * 100.0

    out = {
        "last": round(last, 2),
        "vs_ma50": round((last / ma50 - 1.0) * 100.0, 2),
        "vs_ma200": (round((last / ma200 - 1.0) * 100.0, 2) if ma200 else None),
        "drawdown": round(dd, 2),
        "bucket": drawdown_bucket(dd),
        "above_ma50": last >= ma50,
        "above_ma200": (last >= ma200) if ma200 else None,
    }
    return out


def describe(tk: str, r) -> str:
    """一句话技术面刻画(只描述位置, 不预测方向)。"""
    if not r:
        return ""
    bits = [f"{tk}: 距52周高 {r['drawdown']:+.1f}%({r['bucket']})"]
    bits.append(f"MA50 {r['vs_ma50']:+.1f}%")
    if r["vs_ma200"] is not None:
        bits.append(f"MA200 {r['vs_ma200']:+.1f}%")
    return " · ".join(bits)


def regime(reads: dict) -> dict:
    """把几个大盘指数的读数汇成一个「当前处在什么位置」的结论。

    这是给「大跌概率」做锚的: 都在 MA200 上方且回撤 <5%, 那「熊市已开始」
    需要极强证据; 反之若已跌破 MA200 且回撤 >10%, 先验就该抬高。
    """
    vals = [r for r in reads.values() if r]
    if not vals:
        return {"n": 0, "note": ""}
    n = len(vals)
    above200 = sum(1 for r in vals if r.get("above_ma200"))
    above50 = sum(1 for r in vals if r.get("above_ma50"))
    worst = min(r["drawdown"] for r in vals)
    med_dd = sorted(r["drawdown"] for r in vals)[n // 2]
    if above200 == n and med_dd > -5:
        posture = "全部指数在 200 日线上方且回撤有限 —— 位置上不支持「熊市已开始」"
    elif above200 == 0 and med_dd < -10:
        posture = "指数普遍跌破 200 日线且回撤较深 —— 位置上需要认真对待下行情景"
    elif above200 >= n / 2:
        posture = "多数指数仍在 200 日线上方 —— 更像上升趋势中的回调"
    else:
        posture = "多数指数已跌破 200 日线 —— 趋势结构受损"
    return {"n": n, "above_ma200": above200, "above_ma50": above50,
            "median_drawdown": round(med_dd, 2), "worst_drawdown": round(worst, 2),
            "note": posture}
