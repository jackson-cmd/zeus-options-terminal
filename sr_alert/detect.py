"""盘中触及判定 —— 纯函数,无 IO、无全局状态。

语义严格照搬 ta_screener/engine.py:303 `zone_touch`:

    def zone_touch(low_t, high_t, prev_close, zones):
        # 触及: low_t <= z.hi and high_t >= z.lo
        # 昨收 < zone.lo → 'res';昨收 > zone.hi → 'sup';否则 'inside'
        # 多个命中取 conf 最高(严格 >)

实时版映射:
  · 瞬时价 → low_t = high_t = live_px → 触及化简为 lo <= live_px <= hi(闭区间)。
    EOD 问「今天碰过吗」,实时抓「碰上的那一刻」—— 对告警等价。
  · 'inside' 跳过不报:昨收已在 zone 内,既非支撑测试也非阻力测试。
    engine.py:813 的评分也只认 sup/res(inside 不计分),故与评分引擎行为一致。
    (engine 原注释称 Pine 默认把 inside「归阻力」,但其代码返回 'inside'
     且评分不采纳 → 以评分引擎的真实行为为准。)
  · 「多个命中取 conf 最高」用严格 `>`,本包只喂 conf==3 → 全等 → 保留第一个命中,
    与原口径完全一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

SUP = "sup"   # 昨收在 zone 上方 → 回踩支撑
RES = "res"   # 昨收在 zone 下方 → 上攻阻力
INSIDE = "inside"


@dataclass(frozen=True)
class Alert:
    ticker: str
    kind: str          # SUP | RES
    zone: dict
    price: float
    prev_close: float


def fire_key(ticker: str, zone: dict, day: str) -> tuple:
    """去重键: 同票同 zone 同交易日只报一次。

    用 zone.avg 而非 lo/hi:EOD 重跑后 zone 边界可能微调,avg 更稳。
    含 day → 跨日自动失效,无需清理。
    """
    return (ticker, round(float(zone["avg"]), 4), day)


def classify(prev_close: float, zone: dict) -> str:
    """方向判定 —— 照搬 engine.zone_touch 的三分支,顺序不得调换。"""
    if prev_close < zone["lo"]:
        return RES
    if prev_close > zone["hi"]:
        return SUP
    return INSIDE


def check_entry(ticker: str, prev_close: float, live_px: float,
                zones: list, fired: set, day: str) -> Optional[Alert]:
    """价格进入某个黄金 zone 且方向成立且今日未报过 → 返回 Alert,否则 None。

    zones 必须已筛为 conf==3(见 zones.load_conf3_zones)。
    缺 prev_close / 缺价 / 无 zone → 安全返回 None,不抛。
    """
    if not prev_close or prev_close <= 0:
        return None
    if not live_px or live_px <= 0:
        return None
    for z in (zones or []):
        if not (z["lo"] <= live_px <= z["hi"]):
            continue
        kind = classify(prev_close, z)
        if kind == INSIDE:
            continue
        if fire_key(ticker, z, day) in fired:
            continue
        return Alert(ticker=ticker, kind=kind, zone=z,
                     price=float(live_px), prev_close=float(prev_close))
    return None
