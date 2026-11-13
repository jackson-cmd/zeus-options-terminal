"""双语 seen/saved/deleted 状态合并 —— 纯函数,从 optionstrat_server 抽出以便单测。

Bug 背景:Chrome 扩展每次 /api/bilingual/sync 推自己那份 seen/saved/deleted。旧逻辑对
「服务器有、payload 没有」的 key 在 60s grace 外一律删(为传播扩展的 un-mark)。但扩展的
seen 会随收件箱裁剪(尤其海量 Economist newsletter)→ 已读文章的 seen 被误删 → 在「今天」
里复活成未读。Benzinga 走本地 _digestReadIds 不经此合并,故不受影响。

修复:seen / deleted **单调并集**(drop_missing=False,漏掉也不删 —— 「已读/已删」不该被
一次 sync 遗漏而回滚);saved 保留删传播(drop_missing=True,un-save 是真实操作)。
"""
from __future__ import annotations

_GRACE_MS = 60_000


def merge_ts_map(server_map: dict, incoming_map: dict, payload_updated_at: int,
                 drop_missing: bool = True) -> dict:
    """按 key 取并集,冲突留较大 ts。

    drop_missing=True(默认,saved 用):server 有、incoming 无的 key,当
        server_ts < payload_updated_at - 60s 时删掉(传播扩展的 un-mark),grace 内保留。
    drop_missing=False(seen/deleted 用):server-only 的 key **一律保留**(单调,不复活)。
    """
    out: dict = {}
    incoming_map = incoming_map or {}
    server_map = server_map or {}
    grace_cutoff = int(payload_updated_at or 0) - _GRACE_MS

    for url, ts in incoming_map.items():
        try:
            ts_i = int(ts)
        except Exception:
            continue
        srv = server_map.get(url)
        try:
            ts_s = int(srv) if srv is not None else 0
        except Exception:
            ts_s = 0
        out[url] = ts_i if ts_i >= ts_s else ts_s

    for url, ts in server_map.items():
        if url in out or url in incoming_map:
            continue
        try:
            ts_s = int(ts)
        except Exception:
            continue
        if not drop_missing or ts_s >= grace_cutoff:
            out[url] = ts_s
    return out
