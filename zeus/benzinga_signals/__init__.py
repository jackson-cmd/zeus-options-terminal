"""Benzinga Signals — real-time signal feed as a Benzinga sub-tab.

Backend holds ONE persistent WS to Benzinga (apikey + cookies, no polling → no
429), buffers pushed signal events in a ring, and serves them over a local REST
endpoint the PWA polls. See benzinga_api/signals.py for the cracked WS protocol.

    from benzinga_signals.signals_view import SIGNALS_PANE_HTML, SIGNALS_JS
    from benzinga_signals.signals_api import router as signals_router, start_feed
"""
