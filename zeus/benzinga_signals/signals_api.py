"""Signals REST endpoints over the live WS ring buffer.

GET /api/signals/recent?limit=200  → newest-first buffered signal events
GET /api/signals/status            → {connected, buffered, last_error, seq}

start_feed() launches the persistent WS task — call it from server startup.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from .signals_feed import FEED

router = APIRouter(prefix="/api/signals", tags=["signals"])


def start_feed():
    """Kick the persistent WS task (idempotent). Must run inside the event loop."""
    return FEED.start()


@router.get("/recent")
def recent(limit: int = Query(200, ge=1, le=1000)):
    return {**FEED.status(), "signals": FEED.recent(limit)}


@router.get("/status")
def status():
    return FEED.status()


# Test-only: inject a synthetic signal so the frontend renderer can be exercised
# off-hours (weekends/closed market) when no live events flow.
@router.post("/_debug/inject")
def _debug_inject():
    import time
    FEED._seq += 1
    FEED._add({
        "type": "Signal", "kind": "PRICE_SPIKE_UP",
        "payload": {"symbol": "NVDA", "signal": "Price Spike", "direction": "UP",
                    "securityType": "STOCK", "price": 212.34,
                    "description": "Price Spike UP — synthetic test",
                    "time": time.strftime("%H:%M:%S")},
        "_seq": FEED._seq, "_recv_ms": int(time.time() * 1000),
    })
    return {"ok": True, "buffered": len(FEED.buf)}
