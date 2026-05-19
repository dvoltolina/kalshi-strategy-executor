# tests/test_kalshi_client.py
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization


def generate_test_private_key():
    """Generate a test RSA private key."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    return private_key


def test_create_signature():
    """Signature is generated correctly for a request."""
    from src.kalshi_client import KalshiClient

    private_key = generate_test_private_key()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )

    client = KalshiClient(
        api_key_id="test-key",
        private_key_pem=pem.decode(),
        base_url="https://api.test.com"
    )

    timestamp = "1234567890000"
    method = "GET"
    path = "/trade-api/v2/portfolio/balance"

    signature = client.create_signature(timestamp, method, path)

    # Signature should be base64 encoded
    assert isinstance(signature, str)
    assert len(signature) > 0
    # Should be valid base64
    import base64
    decoded = base64.b64decode(signature)
    assert len(decoded) > 0


def test_get_headers():
    """Headers include all required auth fields."""
    from src.kalshi_client import KalshiClient

    private_key = generate_test_private_key()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )

    client = KalshiClient(
        api_key_id="test-key-123",
        private_key_pem=pem.decode(),
        base_url="https://api.test.com"
    )

    headers = client._get_headers("GET", "/portfolio/balance")

    assert headers["KALSHI-ACCESS-KEY"] == "test-key-123"
    assert "KALSHI-ACCESS-SIGNATURE" in headers
    assert "KALSHI-ACCESS-TIMESTAMP" in headers
    assert headers["Content-Type"] == "application/json"

    # Timestamp should be recent (within last 5 seconds)
    import time
    ts = int(headers["KALSHI-ACCESS-TIMESTAMP"])
    now = int(time.time() * 1000)
    assert abs(now - ts) < 5000


def test_get_exchange_status(mocker):
    """get_exchange_status returns exchange status."""
    from src.kalshi_client import KalshiClient

    private_key = generate_test_private_key()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )

    client = KalshiClient(
        api_key_id="test-key",
        private_key_pem=pem.decode(),
    )

    mock_response = {
        "exchange_active": True,
        "trading_active": True,
    }
    mocker.patch.object(client, "get", return_value=mock_response)

    result = client.get_exchange_status()

    assert result["exchange_active"] is True
    assert result["trading_active"] is True
    client.get.assert_called_once_with("/exchange/status")


def test_get_markets(mocker):
    """get_markets fetches paginated market list."""
    from src.kalshi_client import KalshiClient

    private_key = generate_test_private_key()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )

    client = KalshiClient(api_key_id="test-key", private_key_pem=pem.decode())

    mock_response = {
        "markets": [{"ticker": "TEST-1"}, {"ticker": "TEST-2"}],
        "cursor": "next-page",
    }
    mocker.patch.object(client, "get", return_value=mock_response)

    result = client.get_markets(status="open", limit=100)

    assert len(result["markets"]) == 2
    assert result["cursor"] == "next-page"
    client.get.assert_called_once_with("/markets", params={"status": "open", "limit": 100})


def test_create_order(mocker):
    """create_order submits order with correct parameters."""
    from src.kalshi_client import KalshiClient

    private_key = generate_test_private_key()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )

    client = KalshiClient(api_key_id="test-key", private_key_pem=pem.decode())

    mock_response = {"order": {"order_id": "ord-123", "status": "open"}}
    mocker.patch.object(client, "post", return_value=mock_response)

    result = client.create_order(
        ticker="NFL-TEST",
        side="no",
        action="buy",
        count=10,
        order_type="limit",
        no_price=85,
        client_order_id="client-123",
    )

    assert result["order"]["order_id"] == "ord-123"
    client.post.assert_called_once_with(
        "/portfolio/orders",
        {
            "ticker": "NFL-TEST",
            "side": "no",
            "action": "buy",
            "count": 10,
            "type": "limit",
            "no_price": 85,
            "client_order_id": "client-123",
        },
    )


def test_delete(mocker):
    """delete() makes authenticated DELETE request."""
    from src.kalshi_client import KalshiClient

    private_key = generate_test_private_key()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )

    client = KalshiClient(api_key_id="test-key", private_key_pem=pem.decode())

    mock_response = {}
    mocker.patch.object(client, "_request", return_value=mock_response)

    result = client.delete("/portfolio/orders/ord-123")

    client._request.assert_called_once_with("DELETE", "/portfolio/orders/ord-123", json_data=None)


def test_delete_with_json_body(mocker):
    """delete() passes json_data when provided."""
    from src.kalshi_client import KalshiClient

    private_key = generate_test_private_key()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )

    client = KalshiClient(api_key_id="test-key", private_key_pem=pem.decode())

    mocker.patch.object(client, "_request", return_value={})

    client.delete("/portfolio/orders/batched", json_data={"ids": ["a", "b"]})

    client._request.assert_called_once_with(
        "DELETE", "/portfolio/orders/batched", json_data={"ids": ["a", "b"]}
    )


def test_get_orders(mocker):
    """get_orders fetches resting orders with filters."""
    from src.kalshi_client import KalshiClient

    private_key = generate_test_private_key()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )

    client = KalshiClient(api_key_id="test-key", private_key_pem=pem.decode())

    mock_response = {
        "orders": [{"order_id": "ord-1"}, {"order_id": "ord-2"}],
        "cursor": "",
    }
    mocker.patch.object(client, "get", return_value=mock_response)

    result = client.get_orders(status="resting", limit=50)

    assert len(result["orders"]) == 2
    client.get.assert_called_once_with(
        "/portfolio/orders", params={"limit": 50, "status": "resting"}
    )


def test_get_order(mocker):
    """get_order fetches a single order by ID."""
    from src.kalshi_client import KalshiClient

    private_key = generate_test_private_key()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )

    client = KalshiClient(api_key_id="test-key", private_key_pem=pem.decode())

    mock_response = {
        "order": {
            "order_id": "ord-789",
            "status": "canceled",
            "fill_count": 6,
            "remaining_count": 0,
            "initial_count": 50,
        }
    }
    mocker.patch.object(client, "get", return_value=mock_response)

    result = client.get_order("ord-789")

    assert result["order"]["fill_count"] == 6
    assert result["order"]["initial_count"] == 50
    client.get.assert_called_once_with("/portfolio/orders/ord-789")


def test_cancel_order(mocker):
    """cancel_order sends DELETE for single order."""
    from src.kalshi_client import KalshiClient

    private_key = generate_test_private_key()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )

    client = KalshiClient(api_key_id="test-key", private_key_pem=pem.decode())

    mocker.patch.object(client, "delete", return_value={})

    client.cancel_order("ord-456")

    client.delete.assert_called_once_with("/portfolio/orders/ord-456")


def test_batch_cancel_orders(mocker):
    """batch_cancel_orders sends DELETE with ids array."""
    from src.kalshi_client import KalshiClient

    private_key = generate_test_private_key()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )

    client = KalshiClient(api_key_id="test-key", private_key_pem=pem.decode())

    mocker.patch.object(client, "delete", return_value={})

    client.batch_cancel_orders(["ord-1", "ord-2", "ord-3"])

    client.delete.assert_called_once_with(
        "/portfolio/orders/batched", json_data={"ids": ["ord-1", "ord-2", "ord-3"]}
    )
