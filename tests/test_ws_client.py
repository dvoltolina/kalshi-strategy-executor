# tests/test_ws_client.py
import json
import pytest
from unittest.mock import MagicMock, patch

from src.ws_client import KalshiWebSocket


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.api_key_id = "test-key"
    client.create_signature.return_value = "test-signature"
    return client


@pytest.fixture
def ws(mock_client):
    return KalshiWebSocket(
        client=mock_client,
        on_fill=MagicMock(),
        on_settlement=MagicMock(),
        base_ws_url="wss://test.example.com/ws",
    )


class TestAuthHeaders:
    def test_auth_headers_include_required_fields(self, ws):
        headers = ws._get_auth_headers()
        assert "KALSHI-ACCESS-KEY" in headers
        assert "KALSHI-ACCESS-SIGNATURE" in headers
        assert "KALSHI-ACCESS-TIMESTAMP" in headers
        assert headers["KALSHI-ACCESS-KEY"] == "test-key"
        assert headers["KALSHI-ACCESS-SIGNATURE"] == "test-signature"

    def test_signs_with_ws_path(self, ws, mock_client):
        ws._get_auth_headers()
        mock_client.create_signature.assert_called_once()
        call_args = mock_client.create_signature.call_args
        assert call_args[0][1] == "GET"
        assert call_args[0][2] == "/trade-api/ws/v2"


class TestMessageHandling:
    def test_fill_message_calls_callback(self, ws):
        msg = json.dumps({
            "type": "fill",
            "msg": {"order_id": "ord-123", "count": 5},
        })
        ws._on_message(None, msg)
        ws.on_fill.assert_called_once_with("ord-123", {"order_id": "ord-123", "count": 5})

    def test_fill_without_order_id_ignored(self, ws):
        msg = json.dumps({"type": "fill", "msg": {}})
        ws._on_message(None, msg)
        ws.on_fill.assert_not_called()

    def test_settlement_finalized_calls_callback(self, ws):
        msg = json.dumps({
            "type": "market_lifecycle_v2",
            "msg": {"market_ticker": "MKT-A", "status": "finalized", "result": "yes"},
        })
        ws._on_message(None, msg)
        ws.on_settlement.assert_called_once_with("MKT-A", "yes")

    def test_settlement_settled_calls_callback(self, ws):
        msg = json.dumps({
            "type": "market_lifecycle_v2",
            "msg": {"market_ticker": "MKT-B", "status": "settled", "result": "no"},
        })
        ws._on_message(None, msg)
        ws.on_settlement.assert_called_once_with("MKT-B", "no")

    def test_settlement_open_status_ignored(self, ws):
        msg = json.dumps({
            "type": "market_lifecycle_v2",
            "msg": {"market_ticker": "MKT-C", "status": "open"},
        })
        ws._on_message(None, msg)
        ws.on_settlement.assert_not_called()

    def test_subscribed_message_no_crash(self, ws):
        msg = json.dumps({"type": "subscribed", "id": 1})
        ws._on_message(None, msg)  # Should not crash

    def test_error_message_no_crash(self, ws):
        msg = json.dumps({"type": "error", "msg": "bad request"})
        ws._on_message(None, msg)  # Should not crash

    def test_invalid_json_no_crash(self, ws):
        ws._on_message(None, "not valid json {{{")
        ws.on_fill.assert_not_called()

    def test_fill_callback_error_caught(self, ws):
        ws.on_fill.side_effect = RuntimeError("boom")
        msg = json.dumps({
            "type": "fill",
            "msg": {"order_id": "ord-123", "count": 1},
        })
        # Should not raise
        ws._on_message(None, msg)

    def test_settlement_callback_error_caught(self, ws):
        ws.on_settlement.side_effect = RuntimeError("boom")
        msg = json.dumps({
            "type": "market_lifecycle_v2",
            "msg": {"market_ticker": "MKT-A", "status": "finalized", "result": "no"},
        })
        ws._on_message(None, msg)


class TestOnOpen:
    def test_subscribes_to_channels(self, ws):
        mock_ws = MagicMock()
        ws._on_open(mock_ws)

        assert mock_ws.send.call_count == 2

        calls = [json.loads(c[0][0]) for c in mock_ws.send.call_args_list]
        channels = [c["params"]["channels"][0] for c in calls]
        assert "fill" in channels
        assert "market_lifecycle_v2" in channels

    def test_resets_reconnect_delay(self, ws):
        ws._reconnect_delay = 30.0
        ws._on_open(MagicMock())
        assert ws._reconnect_delay == 1.0
        assert ws._connected is True

    def test_first_connect_no_reconnect_callback(self, ws):
        ws.on_reconnect = MagicMock()
        ws._on_open(MagicMock())
        ws.on_reconnect.assert_not_called()

    def test_second_connect_fires_reconnect_callback(self, ws):
        ws.on_reconnect = MagicMock()
        ws._on_open(MagicMock())  # First connect
        ws._on_open(MagicMock())  # Reconnect
        ws.on_reconnect.assert_called_once()

    def test_reconnect_callback_error_caught(self, ws):
        ws.on_reconnect = MagicMock(side_effect=RuntimeError("boom"))
        ws._on_open(MagicMock())  # First connect
        ws._on_open(MagicMock())  # Reconnect — should not raise


class TestMessageIds:
    def test_ids_increment(self, ws):
        assert ws._next_msg_id() == 1
        assert ws._next_msg_id() == 2
        assert ws._next_msg_id() == 3


class TestStartStop:
    @patch("src.ws_client.websocket.WebSocketApp")
    def test_start_sets_running(self, mock_ws_app, ws):
        # Don't actually start the thread
        with patch.object(ws, "_run"):
            ws.start()
            assert ws._running is True
            ws._running = False  # Cleanup

    def test_stop_sets_not_running(self, ws):
        ws._running = True
        ws._ws = MagicMock()
        ws.stop()
        assert ws._running is False
        assert ws._connected is False

    def test_double_start_noop(self, ws):
        ws._running = True
        ws.start()  # Should return immediately
        assert ws._thread is None  # Never started a thread
