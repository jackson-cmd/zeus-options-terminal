"""Layer 3 —— IV / 财报:IV 太贵(pop 概率低)或财报正好撞在 DTE 窗口里,都别开借方价差。

纯函数:ivr_info/earnings_info 是 data_sources.get_ivr / get_earnings 已经拿好的结果,
本层只做判断,不碰文件。

  · IVR(1年百分位) > 50 → fail(买贵了,后续大概率 IV 回落,拖累借方价差的 P/L)。
  · 财报日 <= DTE + 3 天 → fail(到期前有一次跳空,价差可能被直接打穿)。
  · 两者任一缺数据 → 不阻断该子项(unknown 且按 PASS 处理),只在输出里如实标注 ——
    这是设计里明确要的"不因为数据缺失就拦人",不是偷懒漏判。
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

from . import config


def evaluate(ticker: str, dte: int, ivr_info: dict, earnings_info: dict) -> dict:
    reasons_pass: list[str] = []
    reasons_fail: list[str] = []
    # 展示用:固定自然顺序(IVR→财报),每行自带状态图标,不按 pass/fail 分桶打乱阅读顺序
    lines: list[str] = []

    ivr = ivr_info.get("ivr")
    asof = ivr_info.get("asof")
    if ivr is None:
        msg = "IVR 数据缺失(不在 options_vol 覆盖范围)—— 不阻断,建议开单前手动查一下 IV"
        reasons_pass.append(msg)
        lines.append(msg)
        ivr_ok = True
    else:
        ivr_ok = ivr <= config.IVR_MAX
        stale_note = ""
        if asof:
            try:
                age = (_dt.date.today() - _dt.date.fromisoformat(asof)).days
                if age > config.IVR_STALE_WARN_DAYS:
                    stale_note = f" ⚠️数据截至 {asof},{age} 天未更新,可能过期"
            except Exception:
                pass
        if ivr_ok:
            reasons_pass.append(f"IVR {ivr:.0f}% (阈值 <{config.IVR_MAX:.0f}%){stale_note}")
            lines.append(f"IVR {ivr:.0f}% ✅ (阈值 <{config.IVR_MAX:.0f}%){stale_note}")
        else:
            msg = f"IVR {ivr:.0f}% 偏贵(阈值 <{config.IVR_MAX:.0f}%),IV 回落会拖累借方价差{stale_note}"
            reasons_fail.append(msg)
            lines.append(f"IVR {ivr:.0f}% ❌ 偏贵(阈值 <{config.IVR_MAX:.0f}%),IV 回落会拖累借方价差{stale_note}")

    d2e = earnings_info.get("days_to_earnings")
    earn_date = earnings_info.get("earnings_date")
    if d2e is None:
        msg = "未查到财报日期(ETF 或不在追踪清单内)—— 不阻断,开单前手动确认一下财报日"
        reasons_pass.append(msg)
        lines.append(msg)
        earn_ok = True
    else:
        gate = dte + config.EARNINGS_BUFFER_DAYS
        earn_ok = d2e > gate
        if earn_ok:
            msg = f"距下次财报({earn_date}) {d2e} 天 > DTE+{config.EARNINGS_BUFFER_DAYS} ({gate}天)"
            reasons_pass.append(msg)
            lines.append(f"距下次财报({earn_date}) {d2e} 天 ✅ > DTE+{config.EARNINGS_BUFFER_DAYS} ({gate}天)")
        else:
            msg = f"财报({earn_date})在 {d2e} 天后,落在这单 {gate} 天窗口内 —— 到期前有跳空风险"
            reasons_fail.append(msg)
            lines.append(f"财报({earn_date})在 {d2e} 天后 ❌,落在这单 {gate} 天窗口内 —— 到期前有跳空风险")

    return {
        "pass": bool(ivr_ok and earn_ok),
        "reasons_pass": reasons_pass,
        "reasons_fail": reasons_fail,
        "lines": lines,
        "ivr": ivr, "iv": ivr_info.get("iv"), "ivr_asof": asof,
        "earnings_date": earn_date, "days_to_earnings": d2e,
    }
