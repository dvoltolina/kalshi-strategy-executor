# src/ws_client.py
"""WebSocket client for real-time Kalshi fill and settlement events."""
import json
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING

import websocket

if TYPE_CHECKING:
    from src.kalshi_client import KalshiClient

logger = logging.getLogger(__name__)


class KalshiWebSocket:
    """Persistent WebSocket connection for real-time Kalshi events.

    Subscribes to 'fill' and 'market_lifecycle_v2' channels.
    Runs in a background daemon thread with auto-reconnect.
    """

    def __init__(
        self,
        client: "KalshiClient",
        on_fill: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        on_settlement: Optional[Callable[[str, str], None]] = None,
        on_reconnect: Optional[Callable[[], None]] = None,
        base_ws_url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2",
    ):
        self.client = client
        self.on_fill = on_fill
        self.on_settlement = on_settlement
        self.on_reconnect = on_reconnect
        self.base_ws_url = base_ws_url

        self._running = False
        self._connected = False
        self._thread: Optional[threading.Thread] = None
        self._ws: Optional[websocket.WebSocketApp] = None
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0
        self._msg_id = 0
        self._has_connected_once = False

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self):
        """Start the WebSocket connection in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("WebSocket client started")

    def stop(self):
        """Stop the WebSocket connection."""
        self._running = False
        if self._ws:
            self._ws.close()
        if self._thread:
            self._thread.join(timeout=5)
        self._connected = False
        logger.info("WebSocket client stopped")

    def _run(self):
        """Reconnection loop — runs in background thread."""
        while self._running:
            try:
                headers = self._get_auth_headers()
                self._ws = websocket.WebSocketApp(
                    self.base_ws_url,
                    header=headers,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logger.error(f"WebSocket run error: {e}", exc_info=True)

            self._connected = False
            if self._running:
                logger.info(f"Reconnecting in {self._reconnect_delay:.0f}s...")
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self._max_reconnect_delay
                )

    def _get_auth_headers(self) -> dict:
        """Generate auth headers for the WebSocket upgrade request."""
        timestamp = str(int(time.time() * 1000))
        signature = self.client.create_signature(
            timestamp, "GET", "/trade-api/ws/v2"
        )
        return {
            "KALSHI-ACCESS-KEY": self.client.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }

    def _next_msg_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    def _on_open(self, ws):
        """Connected — subscribe to channels, trigger reconnect callback."""
        is_reconnect = self._has_connected_once
        self._has_connected_once = True
        self._connected = True
        self._reconnect_delay = 1.0
        logger.info("WebSocket connected" + (" (reconnect)" if is_reconnect else ""))

        ws.send(json.dumps({
            "id": self._next_msg_id(),
            "cmd": "subscribe",
            "params": {"channels": ["fill"]},
        }))
        ws.send(json.dumps({
            "id": self._next_msg_id(),
            "cmd": "subscribe",
            "params": {"channels": ["market_lifecycle_v2"]},
        }))
        logger.info("Subscribed to fill and market_lifecycle_v2 channels")

        if is_reconnect and self.on_reconnect:
            try:
                self.on_reconnect()
            except Exception as e:
                logger.error(f"Reconnect callback error: {e}", exc_info=True)

    def _on_message(self, ws, raw: str):
        """Parse and dispatch a WebSocket message."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON from WebSocket: {raw[:200]}", exc_info=True)
            return

        msg_type = msg.get("type")

        if msg_type == "fill":
            self._handle_fill(msg)
        elif msg_type == "market_lifecycle_v2":
            self._handle_lifecycle(msg)
        elif msg_type == "subscribed":
            logger.debug(f"Subscription confirmed: {msg}")
        elif msg_type == "error":
            logger.error(f"WebSocket error message: {msg}")
        else:
            logger.debug(f"Unhandled WS message type: {msg_type}")

    def _on_error(self, ws, error):
        logger.warning(f"WebSocket error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        self._connected = False
        logger.info(f"WebSocket closed (code={close_status_code})")

    def _handle_fill(self, msg: dict):
        """Process a fill notification."""
        data = msg.get("msg", {})
        order_id = data.get("order_id")
        if not order_id:
            return

        logger.info(f"WS fill: order {order_id}")

        if self.on_fill:
            try:
                self.on_fill(order_id, data)
            except Exception as e:
                logger.error(f"Fill callback error: {e}", exc_info=True)

    def _handle_lifecycle(self, msg: dict):
        """Process a market lifecycle/settlement event."""
        data = msg.get("msg", {})
        market_ticker = data.get("market_ticker")
        status = data.get("status")
        result = data.get("result")

        if not market_ticker or status not in ("settled", "finalized"):
            return

        logger.info(f"WS settlement: {market_ticker} -> {result}")

        if self.on_settlement:
            try:
                self.on_settlement(market_ticker, result)
            except Exception as e:
                logger.error(f"Settlement callback error: {e}", exc_info=True)
