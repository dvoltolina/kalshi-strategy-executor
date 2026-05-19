# src/kalshi_client.py
import base64
import logging
import time
from typing import Any, Dict, Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger(__name__)


class KalshiAPIError(Exception):
    """Raised when Kalshi API returns an error."""
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Kalshi API error ({status_code}): {message}")


class KalshiClient:
    """Client for Kalshi API with RSA-PSS authentication."""

    def __init__(
        self,
        api_key_id: str,
        private_key_pem: str,
        base_url: str = "https://api.elections.kalshi.com/trade-api/v2",
        timeout: int = 30,
    ):
        self.api_key_id = api_key_id
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._private_key = serialization.load_pem_private_key(
            private_key_pem.encode(),
            password=None,
        )
        # Extract path prefix from base_url for signing (e.g., /trade-api/v2)
        from urllib.parse import urlparse
        parsed = urlparse(self.base_url)
        self._path_prefix = parsed.path

    def create_signature(self, timestamp: str, method: str, path: str) -> str:
        """Create RSA-PSS signature for request.

        Args:
            timestamp: Unix timestamp in milliseconds as string
            method: HTTP method (GET, POST, etc.)
            path: Request path without query parameters

        Returns:
            Base64 encoded signature
        """
        path_without_query = path.split("?")[0]
        message = f"{timestamp}{method}{path_without_query}".encode("utf-8")

        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

        return base64.b64encode(signature).decode("utf-8")

    def _get_headers(self, method: str, path: str) -> Dict[str, str]:
        """Get authenticated headers for a request."""
        timestamp = str(int(time.time() * 1000))
        # Sign with full path including prefix (e.g., /trade-api/v2/portfolio/balance)
        full_path = f"{self._path_prefix}{path}"
        signature = self.create_signature(timestamp, method, full_path)

        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Make authenticated request to Kalshi API."""
        url = f"{self.base_url}{path}"
        headers = self._get_headers(method, path)

        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            json=json_data,
            timeout=self.timeout,
        )

        if response.status_code >= 400:
            try:
                error_data = response.json()
                message = error_data.get("message", response.text)
            except Exception:
                logger.error("Error parsing API response", exc_info=True)
                message = response.text
            raise KalshiAPIError(response.status_code, message)

        return response.json()

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Make authenticated GET request."""
        return self._request("GET", path, params=params)

    def post(self, path: str, json_data: Dict[str, Any]) -> Dict[str, Any]:
        """Make authenticated POST request."""
        return self._request("POST", path, json_data=json_data)

    def delete(self, path: str, json_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Make authenticated DELETE request."""
        return self._request("DELETE", path, json_data=json_data)

    def get_exchange_status(self) -> Dict[str, Any]:
        """Get exchange status."""
        return self.get("/exchange/status")

    def get_markets(
        self,
        status: str = "open",
        limit: int = 1000,
        cursor: Optional[str] = None,
        min_close_ts: Optional[int] = None,
        max_close_ts: Optional[int] = None,
        mve_filter: Optional[str] = None,
        series_ticker: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get markets with optional filtering.

        Args:
            status: Market status filter ('open', 'closed', 'settled')
            limit: Max results per page (max 1000)
            cursor: Pagination cursor
            min_close_ts: Filter markets closing after this timestamp
            max_close_ts: Filter markets closing before this timestamp
            mve_filter: 'exclude' to skip multivariate events, 'only' for only MVE
            series_ticker: Filter by series ticker
        """
        params = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        if min_close_ts is not None:
            params["min_close_ts"] = min_close_ts
        if max_close_ts is not None:
            params["max_close_ts"] = max_close_ts
        if mve_filter:
            params["mve_filter"] = mve_filter
        if series_ticker:
            params["series_ticker"] = series_ticker
        return self.get("/markets", params=params)

    def get_market(self, ticker: str) -> Dict[str, Any]:
        """Get details for a specific market."""
        return self.get(f"/markets/{ticker}")

    def get_event(self, event_ticker: str) -> Dict[str, Any]:
        """Get details for a specific event."""
        return self.get(f"/events/{event_ticker}")

    def get_series(
        self,
        category: Optional[str] = None,
        tags: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get series list with optional category/tag filtering."""
        params = {}
        if category:
            params["category"] = category
        if tags:
            params["tags"] = tags
        return self.get("/series", params=params)

    def create_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        order_type: str = "limit",
        no_price: Optional[int] = None,
        yes_price: Optional[int] = None,
        client_order_id: Optional[str] = None,
        post_only: bool = False,
    ) -> Dict[str, Any]:
        """Create an order."""
        data: Dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type,
        }
        if no_price is not None:
            data["no_price"] = no_price
        if yes_price is not None:
            data["yes_price"] = yes_price
        if client_order_id:
            data["client_order_id"] = client_order_id
        if post_only:
            data["post_only"] = True

        return self.post("/portfolio/orders", data)

    def get_balance(self) -> Dict[str, Any]:
        """Get account balance."""
        return self.get("/portfolio/balance")

    def get_positions(
        self,
        limit: int = 100,
        cursor: Optional[str] = None,
        count_filter: str = "position",
    ) -> Dict[str, Any]:
        """Get current positions."""
        params = {"limit": limit, "count_filter": count_filter}
        if cursor:
            params["cursor"] = cursor
        return self.get("/portfolio/positions", params=params)

    def get_orders(
        self,
        status: Optional[str] = None,
        ticker: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get orders from portfolio.

        Args:
            status: Filter by status ('resting', 'pending', 'canceled', 'executed', 'partial')
            ticker: Filter by market ticker
            limit: Max results per page (max 1000)
            cursor: Pagination cursor
        """
        params: Dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor
        return self.get("/portfolio/orders", params=params)

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel a single order.

        Args:
            order_id: The order ID to cancel
        """
        return self.delete(f"/portfolio/orders/{order_id}")

    def get_order(self, order_id: str) -> Dict[str, Any]:
        """Get a single order by ID.

        Args:
            order_id: The order ID to fetch

        Returns:
            Dict with order details including fill_count, remaining_count, etc.
        """
        return self.get(f"/portfolio/orders/{order_id}")

    def get_milestones(self, event_ticker: str, limit: int = 5) -> Dict[str, Any]:
        """Fetch milestones for an event (includes start_date)."""
        return self.get("/milestones", params={
            "related_event_ticker": event_ticker,
            "limit": limit,
        })

    def batch_cancel_orders(self, order_ids: list) -> Dict[str, Any]:
        """Cancel multiple orders in a batch (max 20).

        Args:
            order_ids: List of order IDs to cancel (max 20)
        """
        return self.delete("/portfolio/orders/batched", json_data={"ids": order_ids})
