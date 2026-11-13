"""AI 选股 HTTP 端点。register(app) 挂到 optionstrat_server 的 FastAPI app 上。

隔离设计: 全部 job 状态/逻辑在本模块, 对 optionstrat_server.py 只需 1 处
    from ai_picks.routes import register; register(app)
"""
from __future__ import annotations
import asyncio, datetime
from fastapi import Request

from . import runner, store

# 单例后台 job 状态(供前端轮询)
_JOB: dict = {"running": False, "pct": 0, "n_total": 0, "n_done": 0,
              "error": None, "date": None, "started": None, "finished": None}
_TASK = None


def _today() -> str:
    return datetime.date.today().isoformat()


async def _run_job(date: str, notify_on_done: bool):
    loop = asyncio.get_event_loop()
    try:
        # 阻塞式 run 丢到线程池(内部自带 40 并发子进程)
        await loop.run_in_executor(
            None, lambda: runner.run(date=date, notify_on_done=notify_on_done,
                                     progress=_JOB, log=lambda *a: None))
    except Exception as e:
        _JOB["error"] = str(e)[:300]
        _JOB["running"] = False


def register(app):
    @app.post("/api/ai_picks/run")
    async def ai_picks_run(req: Request):
        try:
            body = await req.json()
        except Exception:
            body = {}
        mode = (body or {}).get("mode") or "manual"
        date = _today()
        if _JOB.get("running"):
            return {"status": "running", "date": _JOB.get("date")}
        # auto 模式: 当天已有缓存则不重复跑
        if mode == "auto" and store.load(date):
            return {"status": "done", "date": date}
        global _TASK
        _JOB.update(running=True, pct=0, n_total=0, n_done=0, error=None,
                    date=date, started=datetime.datetime.now().isoformat(timespec="seconds"),
                    finished=None)
        _TASK = asyncio.create_task(_run_job(date, notify_on_done=(mode == "auto")))
        return {"status": "running", "date": date}

    @app.get("/api/ai_picks/status")
    def ai_picks_status():
        return {k: _JOB.get(k) for k in
                ("running", "pct", "n_total", "n_done", "error", "date", "started", "finished")}

    @app.get("/api/ai_picks")
    def ai_picks_get(date: str = ""):
        data = store.load(date) if date else (store.load(_today()) or store.latest())
        if not data:
            return {"date": date or _today(), "picks": {}, "empty": True}
        return data

    return app
