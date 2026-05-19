# tests/test_order_guard.py
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.order_guard import OrderGuard, EventCache


# -- EventCache tests --

def _make_mock_db(rows=None):
    """Create a mock DB for EventCache."""
    db = MagicMock()
    db.load_event_estimates.return_value = rows or []
    return db


class TestEventCache:
    def test_put_and_get(self):
        db = _make_mock_db()
        cache = EventCache(db)
        now = datetime.now(timezone.utc)
        cache.put("EVT-1", {
            "estimated_start_utc": now + timedelta(hours=1),
            "confidence": "high",
        })
        assert cache.get("EVT-1") is not None
        assert cache.get("EVT-1")["confidence"] == "high"

    def test_get_miss(self):
        cache = EventCache(_make_mock_db())
        assert cache.get("NONEXISTENT") is None

    def test_cached_permanently(self):
        """Once cached, estimates are always returned (no staleness re-query)."""
        cache = EventCache(_make_mock_db())
        now = datetime.now(timezone.utc)
        cache.put("EVT-FAR", {
            "estimated_start_utc": now + timedelta(hours=10),
            "confidence": "medium",
        })
        # Force very old cached_at — should still be returned
        cache._cache["EVT-FAR"]["cached_at"] = now - timedelta(hours=48)

        assert cache.get("EVT-FAR") is not None
        assert cache.get("EVT-FAR")["confidence"] == "medium"

    def test_len(self):
        cache = EventCache(_make_mock_db())
        assert len(cache) == 0
        cache.put("A", {"estimated_start_utc": None, "confidence": "low"})
        cache.put("B", {"estimated_start_utc": None, "confidence": "low"})
        assert len(cache) == 2

    def test_put_persists_to_db(self):
        db = _make_mock_db()
        cache = EventCache(db)
        now = datetime.now(timezone.utc)
        start = now + timedelta(hours=2)

        cache.put("EVT-PERSIST", {
            "estimated_start_utc": start,
            "confidence": "high",
            "reasoning": "test reason",
            "event_title": "Lakers vs Rockets NBA Mentions",
        })

        db.save_event_estimate.assert_called_once()
        call_kwargs = db.save_event_estimate.call_args[1]
        assert call_kwargs["event_ticker"] == "EVT-PERSIST"
        assert call_kwargs["confidence"] == "high"
        assert call_kwargs["reasoning"] == "test reason"
        assert call_kwargs["event_title"] == "Lakers vs Rockets NBA Mentions"

    def test_loads_from_db_on_init(self):
        now = datetime.now(timezone.utc)
        start = now + timedelta(hours=3)
        db = _make_mock_db(rows=[{
            "event_ticker": "EVT-LOADED",
            "estimated_start_utc": start.isoformat(),
            "confidence": "medium",
            "reasoning": "from db",
            "cached_at": now.isoformat(),
        }])

        cache = EventCache(db)

        result = cache.get("EVT-LOADED")
        assert result is not None
        assert result["confidence"] == "medium"
        assert result["reasoning"] == "from db"

    def test_skips_invalid_db_rows(self):
        db = _make_mock_db(rows=[{
            "event_ticker": "EVT-BAD",
            "estimated_start_utc": "not-a-date",
            "confidence": "high",
            "cached_at": "also-not-a-date",
        }])

        cache = EventCache(db)
        assert cache.get("EVT-BAD") is None
        assert len(cache) == 0


# -- OrderGuard tests --

def make_guard(dry_run=False, min_confidence="medium", cancel_buffer=5,
               pending_order_ids=None, strategies=None):
    """Create an OrderGuard with mocked dependencies."""
    client = MagicMock()
    db = MagicMock()
    db.load_event_estimates.return_value = []
    db.cleanup_old_estimates.return_value = 0

    if pending_order_ids is None:
        pending_order_ids = {f"ord-{i}" for i in range(100)}
    db.get_pending_order_ids.return_value = pending_order_ids

    # Default: milestones returns empty so existing tests fall through to Grok
    client.get_milestones.return_value = {"milestones": []}

    guard = OrderGuard(
        client=client,
        db=db,
        grok_api_key="test-key",  # pragma: allowlist secret
        cancel_buffer_minutes=cancel_buffer,
        min_confidence=min_confidence,
        dry_run=dry_run,
        strategies=strategies,
    )
    return guard


def _make_grok_response(estimates):
    """Build a mock Grok /v1/responses JSON response with a function_call."""
    return {
        "output": [{
            "type": "function_call",
            "name": "report_event_start_times",
            "arguments": json.dumps({"events": estimates}),
            "call_id": "call_test123",
        }],
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }


def _mock_grok_post(response_json):
    """Create a mock requests.post return value with the given JSON body."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = response_json
    mock_resp.raise_for_status.return_value = None
    return mock_resp


class TestOrderGuard:
    def test_no_resting_orders(self):
        guard = make_guard()
        guard.client.get_orders.return_value = {"orders": [], "cursor": ""}

        stats = guard.run_cycle()

        assert stats["resting_orders"] == 0
        assert stats["orders_cancelled"] == 0

    def test_no_pending_orders_in_database(self):
        guard = make_guard(pending_order_ids=set())

        stats = guard.run_cycle()

        assert stats["resting_orders"] == 0
        guard.client.get_orders.assert_not_called()

    def test_filters_to_strategy_orders_only(self):
        guard = make_guard(pending_order_ids={"ord-mentions"})
        now = datetime.now(timezone.utc)

        guard.client.get_orders.return_value = {
            "orders": [
                {"order_id": "ord-mentions", "ticker": "MKT-A"},
                {"order_id": "ord-longshot", "ticker": "MKT-B"},
                {"order_id": "ord-manual", "ticker": "MKT-C"},
            ],
            "cursor": "",
        }

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-1",
                "title": "Test Event",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }
        }

        guard.event_cache.put("EVT-1", {
            "estimated_start_utc": now + timedelta(hours=10),
            "confidence": "high",
        })

        stats = guard.run_cycle()

        assert stats["resting_orders"] == 1

    def test_cancel_imminent_event(self):
        guard = make_guard()
        now = datetime.now(timezone.utc)

        guard.client.get_orders.return_value = {
            "orders": [
                {"order_id": "ord-1", "ticker": "MKT-A", "initial_count": 50},
                {"order_id": "ord-2", "ticker": "MKT-A", "initial_count": 50},
            ],
            "cursor": "",
        }

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-NBA",
                "title": "Lakers vs Celtics Mentions",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }
        }

        guard.event_cache.put("EVT-NBA", {
            "estimated_start_utc": now + timedelta(minutes=2),
            "confidence": "high",
        })

        guard.client.batch_cancel_orders.return_value = {}
        guard.client.get_order.side_effect = [
            {"order": {"order_id": "ord-1", "fill_count": 0, "initial_count": 50}},
            {"order": {"order_id": "ord-2", "fill_count": 0, "initial_count": 50}},
        ]

        stats = guard.run_cycle()

        assert stats["resting_orders"] == 2
        assert stats["orders_cancelled"] == 2
        guard.client.batch_cancel_orders.assert_called_once_with(["ord-1", "ord-2"])

    def test_cancel_marks_orders_cancelled_in_db(self):
        guard = make_guard()
        now = datetime.now(timezone.utc)

        guard.client.get_orders.return_value = {
            "orders": [
                {"order_id": "ord-1", "ticker": "MKT-A", "initial_count": 50},
                {"order_id": "ord-2", "ticker": "MKT-A", "initial_count": 50},
            ],
            "cursor": "",
        }

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-DB",
                "title": "DB Update Event",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }
        }

        guard.event_cache.put("EVT-DB", {
            "estimated_start_utc": now + timedelta(minutes=1),
            "confidence": "high",
        })

        guard.client.batch_cancel_orders.return_value = {}
        guard.client.get_order.side_effect = [
            {"order": {"order_id": "ord-1", "fill_count": 3, "initial_count": 50}},
            {"order": {"order_id": "ord-2", "fill_count": 0, "initial_count": 50}},
        ]

        guard.run_cycle()

        # Verify DB was updated for each cancelled order with fill counts
        calls = guard.db.update_order_status.call_args_list
        assert len(calls) == 2
        assert calls[0][0] == ("ord-1", "cancelled")
        assert calls[0][1] == {"filled_quantity": 3}
        assert calls[1][0] == ("ord-2", "cancelled")
        assert calls[1][1] == {"filled_quantity": 0}

    def test_skip_far_future_event(self):
        guard = make_guard()
        now = datetime.now(timezone.utc)

        guard.client.get_orders.return_value = {
            "orders": [{"order_id": "ord-1", "ticker": "MKT-A"}],
            "cursor": "",
        }

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-LATE",
                "title": "Some Event Tomorrow",
                "close_time": (now + timedelta(hours=20)).isoformat(),
            }
        }

        guard.event_cache.put("EVT-LATE", {
            "estimated_start_utc": now + timedelta(hours=18),
            "confidence": "high",
        })

        stats = guard.run_cycle()

        assert stats["orders_cancelled"] == 0
        guard.client.batch_cancel_orders.assert_not_called()
        guard.client.cancel_order.assert_not_called()

    def test_skip_low_confidence(self):
        guard = make_guard(min_confidence="medium")
        now = datetime.now(timezone.utc)

        guard.client.get_orders.return_value = {
            "orders": [{"order_id": "ord-1", "ticker": "MKT-A"}],
            "cursor": "",
        }

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-LOW",
                "title": "Some Unclear Event",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }
        }

        guard.event_cache.put("EVT-LOW", {
            "estimated_start_utc": now + timedelta(minutes=1),
            "confidence": "low",
        })

        stats = guard.run_cycle()

        assert stats["orders_cancelled"] == 0

    def test_past_event_forces_cancel_regardless_of_confidence(self):
        """Events that already started should be cancelled even with low confidence."""
        guard = make_guard(min_confidence="medium")
        now = datetime.now(timezone.utc)

        guard.client.get_orders.return_value = {
            "orders": [{"order_id": "ord-1", "ticker": "MKT-A", "initial_count": 50}],
            "cursor": "",
        }

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-PAST",
                "title": "Already Started Event",
                "close_time": (now + timedelta(hours=1)).isoformat(),
            }
        }

        # Event started 10 minutes ago, but confidence is low
        guard.event_cache.put("EVT-PAST", {
            "estimated_start_utc": now - timedelta(minutes=10),
            "confidence": "low",
        })

        guard.client.batch_cancel_orders.return_value = {}
        guard.client.get_order.return_value = {
            "order": {"fill_count": 0, "initial_count": 50}
        }

        stats = guard.run_cycle()

        assert stats["orders_cancelled"] == 1

    @patch("src.order_guard.http_requests.post")
    def test_errors_stat_incremented(self, mock_post):
        guard = make_guard()
        now = datetime.now(timezone.utc)

        guard.client.get_orders.return_value = {
            "orders": [{"order_id": "ord-1", "ticker": "MKT-A"}],
            "cursor": "",
        }

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-ERR",
                "title": "Error Event",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }
        }

        guard.client.get_event.return_value = {
            "event": {"title": "Error Event", "subtitle": "", "category": ""}
        }

        mock_post.side_effect = Exception("API down")

        stats = guard.run_cycle()

        assert stats["errors"] >= 1

    def test_market_cache_cleared_between_cycles(self):
        guard = make_guard()
        now = datetime.now(timezone.utc)

        guard.client.get_orders.return_value = {
            "orders": [{"order_id": "ord-1", "ticker": "MKT-A"}],
            "cursor": "",
        }

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-CACHE",
                "title": "Cache Test",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }
        }

        guard.event_cache.put("EVT-CACHE", {
            "estimated_start_utc": now + timedelta(hours=10),
            "confidence": "high",
        })

        guard.run_cycle()
        # Market cache should have been populated
        assert guard.client.get_market.call_count == 1

        # Run second cycle - market cache should be cleared, so get_market called again
        guard.run_cycle()
        assert guard.client.get_market.call_count == 2

    def test_dry_run_no_cancel(self):
        guard = make_guard(dry_run=True)
        now = datetime.now(timezone.utc)

        guard.client.get_orders.return_value = {
            "orders": [{"order_id": "ord-1", "ticker": "MKT-A"}],
            "cursor": "",
        }

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-DRY",
                "title": "Test Event",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }
        }

        guard.event_cache.put("EVT-DRY", {
            "estimated_start_utc": now + timedelta(minutes=1),
            "confidence": "high",
        })

        stats = guard.run_cycle()

        assert stats["orders_cancelled"] == 1
        guard.client.batch_cancel_orders.assert_not_called()
        guard.client.cancel_order.assert_not_called()

    def test_batch_splitting(self):
        guard = make_guard()
        now = datetime.now(timezone.utc)

        orders = [{"order_id": f"ord-{i}", "ticker": "MKT-A", "initial_count": 50} for i in range(25)]
        guard.client.get_orders.return_value = {"orders": orders, "cursor": ""}

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-BIG",
                "title": "Big Event",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }
        }

        guard.event_cache.put("EVT-BIG", {
            "estimated_start_utc": now + timedelta(minutes=1),
            "confidence": "high",
        })

        guard.client.batch_cancel_orders.return_value = {}
        guard.client.get_order.return_value = {
            "order": {"fill_count": 0, "initial_count": 50}
        }

        stats = guard.run_cycle()

        assert stats["orders_cancelled"] == 25
        assert guard.client.batch_cancel_orders.call_count == 2
        first_call_ids = guard.client.batch_cancel_orders.call_args_list[0][0][0]
        second_call_ids = guard.client.batch_cancel_orders.call_args_list[1][0][0]
        assert len(first_call_ids) == 20
        assert len(second_call_ids) == 5

    def test_batch_cancel_fallback_to_individual(self):
        guard = make_guard()
        now = datetime.now(timezone.utc)

        guard.client.get_orders.return_value = {
            "orders": [
                {"order_id": "ord-1", "ticker": "MKT-A", "initial_count": 50},
                {"order_id": "ord-2", "ticker": "MKT-A", "initial_count": 50},
            ],
            "cursor": "",
        }

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-FAIL",
                "title": "Failing Batch",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }
        }

        guard.event_cache.put("EVT-FAIL", {
            "estimated_start_utc": now + timedelta(minutes=1),
            "confidence": "high",
        })

        guard.client.batch_cancel_orders.side_effect = Exception("batch failed")
        guard.client.cancel_order.side_effect = [
            {"order": {"order_id": "ord-1", "fill_count": 6, "initial_count": 50}},
            {"order": {"order_id": "ord-2", "fill_count": 0, "initial_count": 50}},
        ]

        stats = guard.run_cycle()

        assert stats["orders_cancelled"] == 2
        assert stats["filled_contracts"] == 6
        assert guard.client.cancel_order.call_count == 2

    @patch("src.order_guard.http_requests.post")
    def test_cache_reuse(self, mock_post):
        guard = make_guard()
        now = datetime.now(timezone.utc)

        guard.client.get_orders.return_value = {
            "orders": [{"order_id": "ord-1", "ticker": "MKT-A"}],
            "cursor": "",
        }

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-CACHED",
                "title": "Already Cached",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }
        }

        guard.event_cache.put("EVT-CACHED", {
            "estimated_start_utc": now + timedelta(hours=2),
            "confidence": "high",
        })

        stats = guard.run_cycle()

        assert stats["events_checked"] == 0
        mock_post.assert_not_called()

    @patch("src.order_guard.http_requests.post")
    def test_no_tool_use_block_cancels_as_precaution(self, mock_post):
        """When Grok doesn't call the tool, orders are cancelled as precaution."""
        guard = make_guard()
        now = datetime.now(timezone.utc)

        guard.client.get_orders.return_value = {
            "orders": [{"order_id": "ord-1", "ticker": "MKT-A", "initial_count": 50}],
            "cursor": "",
        }

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-BAD",
                "title": "Bad Response Event",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }
        }

        guard.client.get_event.return_value = {
            "event": {"title": "Bad Response Event", "subtitle": "", "category": ""}
        }

        # Grok responds with text only, no function_call
        mock_post.return_value = _mock_grok_post({
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "I can't determine this"}]}],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        })

        guard.client.batch_cancel_orders.return_value = {}
        guard.client.get_order.return_value = {
            "order": {"fill_count": 0, "initial_count": 50}
        }

        stats = guard.run_cycle()

        assert stats["orders_cancelled"] == 1

    @patch("src.order_guard.http_requests.post")
    def test_get_event_called_for_uncached_events(self, mock_post):
        """get_event() is called for each uncached event to fetch rich data."""
        guard = make_guard()
        now = datetime.now(timezone.utc)
        start_far = now + timedelta(hours=10)

        guard.client.get_orders.return_value = {
            "orders": [
                {"order_id": "ord-1", "ticker": "MKT-A"},
                {"order_id": "ord-2", "ticker": "MKT-B"},
            ],
            "cursor": "",
        }

        guard.client.get_market.side_effect = [
            {"market": {
                "event_ticker": "EVT-1",
                "title": "Will announcer say airball?",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }},
            {"market": {
                "event_ticker": "EVT-1",
                "title": "Will announcer say dagger?",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }},
        ]

        guard.client.get_event.return_value = {
            "event": {
                "title": "Lakers vs Rockets NBA Mentions",
                "subtitle": "Feb 10",
                "category": "NBA",
            }
        }

        mock_post.return_value = _mock_grok_post(_make_grok_response([{
            "event_ticker": "EVT-1",
            "estimated_start_utc": start_far.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "confidence": "high",
            "reasoning": "NBA game tonight",
        }]))

        guard.run_cycle()

        guard.client.get_event.assert_called_once_with("EVT-1")

    @patch("src.order_guard.http_requests.post")
    def test_grok_prompt_includes_rich_event_data(self, mock_post):
        """Grok prompt includes event title, subtitle, category, market titles, today's date."""
        guard = make_guard()
        now = datetime.now(timezone.utc)
        start_far = now + timedelta(hours=10)

        guard.client.get_orders.return_value = {
            "orders": [
                {"order_id": "ord-1", "ticker": "MKT-A"},
                {"order_id": "ord-2", "ticker": "MKT-B"},
            ],
            "cursor": "",
        }

        guard.client.get_market.side_effect = [
            {"market": {
                "event_ticker": "EVT-1",
                "title": "Will announcer say airball?",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }},
            {"market": {
                "event_ticker": "EVT-1",
                "title": "Will announcer say dagger?",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }},
        ]

        guard.client.get_event.return_value = {
            "event": {
                "title": "Lakers vs Rockets NBA Mentions",
                "subtitle": "Feb 10",
                "category": "NBA",
            }
        }

        mock_post.return_value = _mock_grok_post(_make_grok_response([{
            "event_ticker": "EVT-1",
            "estimated_start_utc": start_far.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "confidence": "high",
            "reasoning": "NBA game tonight",
        }]))

        guard.run_cycle()

        # Verify the Grok prompt content
        call_kwargs = mock_post.call_args[1]
        user_msg = call_kwargs["json"]["input"][0]["content"]

        assert "Today's date:" in user_msg
        assert "NEXT future instance" in user_msg
        assert "Lakers vs Rockets NBA Mentions" in user_msg
        assert "Will announcer say airball?" in user_msg
        assert "Will announcer say dagger?" in user_msg
        assert '"category": "NBA"' in user_msg
        assert '"subtitle": "Feb 10"' in user_msg

    @patch("src.order_guard.http_requests.post")
    def test_event_title_stored_in_cache(self, mock_post):
        """event_title from get_event() is stored in cache alongside estimate."""
        guard = make_guard()
        now = datetime.now(timezone.utc)
        start_far = now + timedelta(hours=10)

        guard.client.get_orders.return_value = {
            "orders": [{"order_id": "ord-1", "ticker": "MKT-A"}],
            "cursor": "",
        }

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-TITLE",
                "title": "Will announcer say airball?",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }
        }

        guard.client.get_event.return_value = {
            "event": {
                "title": "Lakers vs Rockets NBA Mentions",
                "subtitle": "",
                "category": "NBA",
            }
        }

        mock_post.return_value = _mock_grok_post(_make_grok_response([{
            "event_ticker": "EVT-TITLE",
            "estimated_start_utc": start_far.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "confidence": "high",
            "reasoning": "NBA game",
        }]))

        guard.run_cycle()

        cached = guard.event_cache.get("EVT-TITLE")
        assert cached is not None
        assert cached["event_title"] == "Lakers vs Rockets NBA Mentions"

    @patch("src.order_guard.http_requests.post")
    def test_grok_tool_use_valid_estimates(self, mock_post):
        guard = make_guard()
        now = datetime.now(timezone.utc)
        start_soon = now + timedelta(minutes=3)

        guard.client.get_orders.return_value = {
            "orders": [{"order_id": "ord-1", "ticker": "MKT-A", "initial_count": 50}],
            "cursor": "",
        }

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-GROK",
                "title": "Lakers vs Celtics 7:30 PM ET",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }
        }

        guard.client.get_event.return_value = {
            "event": {
                "title": "Lakers vs Celtics NBA Mentions",
                "subtitle": "",
                "category": "NBA",
            }
        }

        mock_post.return_value = _mock_grok_post(_make_grok_response([{
            "event_ticker": "EVT-GROK",
            "estimated_start_utc": start_soon.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "confidence": "high",
            "reasoning": "Title says 7:30 PM ET",
        }]))

        guard.client.batch_cancel_orders.return_value = {}
        guard.client.get_order.return_value = {
            "order": {"fill_count": 0, "initial_count": 50}
        }

        stats = guard.run_cycle()

        assert stats["events_checked"] == 1
        assert stats["orders_cancelled"] == 1

    @patch("src.order_guard.http_requests.post")
    def test_grok_api_error_cancels_as_precaution(self, mock_post):
        """When Grok API errors out, orders are cancelled as precaution."""
        guard = make_guard()
        now = datetime.now(timezone.utc)

        guard.client.get_orders.return_value = {
            "orders": [{"order_id": "ord-1", "ticker": "MKT-A", "initial_count": 50}],
            "cursor": "",
        }

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-ERR",
                "title": "Error Event",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }
        }

        guard.client.get_event.return_value = {
            "event": {"title": "Error Event", "subtitle": "", "category": ""}
        }

        mock_post.side_effect = Exception("API down")

        guard.client.batch_cancel_orders.return_value = {}
        guard.client.get_order.return_value = {
            "order": {"fill_count": 0, "initial_count": 50}
        }

        stats = guard.run_cycle()

        assert stats["orders_cancelled"] == 1

    def test_pagination(self):
        guard = make_guard()
        now = datetime.now(timezone.utc)

        guard.client.get_orders.side_effect = [
            {
                "orders": [{"order_id": "ord-1", "ticker": "MKT-A"}],
                "cursor": "page2",
            },
            {
                "orders": [{"order_id": "ord-2", "ticker": "MKT-A"}],
                "cursor": "",
            },
        ]

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-PAGE",
                "title": "Paged Event",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }
        }

        guard.event_cache.put("EVT-PAGE", {
            "estimated_start_utc": now + timedelta(hours=10),
            "confidence": "high",
        })

        stats = guard.run_cycle()

        assert stats["resting_orders"] == 2
        assert guard.client.get_orders.call_count == 2

    def test_cleanup_on_init(self):
        """Old estimates are cleaned up when OrderGuard is created."""
        client = MagicMock()
        db = MagicMock()
        db.load_event_estimates.return_value = []
        db.cleanup_old_estimates.return_value = 3

        OrderGuard(
            client=client,
            db=db,
            grok_api_key="test-key",  # pragma: allowlist secret
        )

        db.cleanup_old_estimates.assert_called_once()


class TestDatabaseIntegration:
    def test_get_pending_order_ids(self, tmp_path):
        from src.database import Database

        db = Database(str(tmp_path / "test.db"))

        db.record_order(
            client_order_id="client-1", order_id="kalshi-ord-1",
            strategy="mentions", ticker="MKT-A", side="no", action="buy",
            price_cents=65, quantity=10, status="pending",
            market_snapshot={"title": "Test"},
        )
        db.record_order(
            client_order_id="client-2", order_id="kalshi-ord-2",
            strategy="longshot", ticker="MKT-B", side="no", action="buy",
            price_cents=90, quantity=10, status="pending",
            market_snapshot={"title": "Test 2"},
        )
        db.record_order(
            client_order_id="client-3", order_id="kalshi-ord-3",
            strategy="mentions", ticker="MKT-C", side="no", action="buy",
            price_cents=70, quantity=10, status="pending",
            market_snapshot={"title": "Test 3"},
        )
        db.record_settlement("client-3", "MKT-C", "no", 300)

        result = db.get_pending_order_ids(strategy="mentions")

        assert result == {"kalshi-ord-1"}
        db.close()

    def test_event_estimate_persistence(self, tmp_path):
        """Estimates survive DB close/reopen (simulating process restart)."""
        from src.database import Database

        db_path = str(tmp_path / "test.db")
        now = datetime.now(timezone.utc)
        start = now + timedelta(hours=2)

        # Save an estimate
        db = Database(db_path)
        db.save_event_estimate(
            event_ticker="EVT-PERSIST",
            estimated_start_utc=start.isoformat(),
            confidence="high",
            reasoning="NBA game at 7:30 PM",
            event_title="Lakers vs Rockets NBA Mentions",
            cached_at=now.isoformat(),
        )
        db.close()

        # Reopen and load
        db2 = Database(db_path)
        rows = db2.load_event_estimates()

        assert len(rows) == 1
        assert rows[0]["event_ticker"] == "EVT-PERSIST"
        assert rows[0]["confidence"] == "high"
        assert rows[0]["reasoning"] == "NBA game at 7:30 PM"
        assert rows[0]["event_title"] == "Lakers vs Rockets NBA Mentions"
        db2.close()

    def test_cleanup_old_estimates(self, tmp_path):
        """Old estimates are removed, recent ones kept."""
        from src.database import Database

        db = Database(str(tmp_path / "test.db"))
        now = datetime.now(timezone.utc)

        # Old event (yesterday)
        db.save_event_estimate(
            event_ticker="EVT-OLD",
            estimated_start_utc=(now - timedelta(hours=30)).isoformat(),
            confidence="high", reasoning="old", cached_at=now.isoformat(),
        )
        # Recent event (tomorrow)
        db.save_event_estimate(
            event_ticker="EVT-NEW",
            estimated_start_utc=(now + timedelta(hours=20)).isoformat(),
            confidence="medium", reasoning="new", cached_at=now.isoformat(),
        )

        cutoff = (now - timedelta(hours=24)).isoformat()
        deleted = db.cleanup_old_estimates(cutoff)

        assert deleted == 1
        rows = db.load_event_estimates()
        assert len(rows) == 1
        assert rows[0]["event_ticker"] == "EVT-NEW"
        db.close()

    def test_update_order_status_and_exclusion(self, tmp_path):
        """Cancelled orders are excluded from get_pending_order_ids."""
        from src.database import Database

        db = Database(str(tmp_path / "test.db"))

        db.record_order(
            client_order_id="client-1", order_id="kalshi-ord-1",
            strategy="mentions", ticker="MKT-A", side="no", action="buy",
            price_cents=65, quantity=10, status="pending",
            market_snapshot={"title": "Test"},
        )
        db.record_order(
            client_order_id="client-2", order_id="kalshi-ord-2",
            strategy="mentions", ticker="MKT-B", side="no", action="buy",
            price_cents=70, quantity=10, status="pending",
            market_snapshot={"title": "Test 2"},
        )

        # Both should be pending
        assert db.get_pending_order_ids("mentions") == {"kalshi-ord-1", "kalshi-ord-2"}

        # Cancel one
        db.update_order_status("kalshi-ord-1", "cancelled")

        # Only the non-cancelled one remains
        assert db.get_pending_order_ids("mentions") == {"kalshi-ord-2"}
        db.close()

    def test_event_cache_loads_from_real_db(self, tmp_path):
        """EventCache loads persisted estimates on init."""
        from src.database import Database

        db_path = str(tmp_path / "test.db")
        now = datetime.now(timezone.utc)
        start = now + timedelta(hours=1)

        db = Database(db_path)
        db.save_event_estimate(
            event_ticker="EVT-RESTART",
            estimated_start_utc=start.isoformat(),
            confidence="high",
            reasoning="survives restart",
            cached_at=now.isoformat(),
        )

        cache = EventCache(db)

        result = cache.get("EVT-RESTART")
        assert result is not None
        assert result["confidence"] == "high"
        db.close()

    def test_update_order_status_with_filled_quantity(self, tmp_path):
        """update_order_status records filled_quantity when provided."""
        from src.database import Database

        db = Database(str(tmp_path / "test.db"))

        db.record_order(
            client_order_id="client-1", order_id="kalshi-ord-1",
            strategy="mentions", ticker="MKT-A", side="no", action="buy",
            price_cents=65, quantity=50, status="pending",
            market_snapshot={"title": "Test"},
        )

        db.update_order_status("kalshi-ord-1", "cancelled", filled_quantity=6)

        row = db.conn.execute(
            "SELECT status, filled_quantity FROM orders WHERE order_id = ?",
            ("kalshi-ord-1",),
        ).fetchone()

        assert row["status"] == "cancelled"
        assert row["filled_quantity"] == 6
        db.close()

    def test_update_order_status_without_filled_quantity(self, tmp_path):
        """update_order_status leaves filled_quantity NULL when not provided."""
        from src.database import Database

        db = Database(str(tmp_path / "test.db"))

        db.record_order(
            client_order_id="client-1", order_id="kalshi-ord-1",
            strategy="mentions", ticker="MKT-A", side="no", action="buy",
            price_cents=65, quantity=50, status="pending",
            market_snapshot={"title": "Test"},
        )

        db.update_order_status("kalshi-ord-1", "cancelled")

        row = db.conn.execute(
            "SELECT status, filled_quantity FROM orders WHERE order_id = ?",
            ("kalshi-ord-1",),
        ).fetchone()

        assert row["status"] == "cancelled"
        assert row["filled_quantity"] is None
        db.close()


class TestPartialFillTracking:
    def test_batch_cancel_tracks_partial_fills(self):
        """Batch cancel fetches fill counts via get_order after batch succeeds."""
        guard = make_guard()
        now = datetime.now(timezone.utc)

        guard.client.get_orders.return_value = {
            "orders": [
                {"order_id": "ord-1", "ticker": "MKT-A", "initial_count": 50},
                {"order_id": "ord-2", "ticker": "MKT-A", "initial_count": 50},
            ],
            "cursor": "",
        }

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-FILL",
                "title": "Partial Fill Event",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }
        }

        guard.event_cache.put("EVT-FILL", {
            "estimated_start_utc": now + timedelta(minutes=1),
            "confidence": "high",
        })

        guard.client.batch_cancel_orders.return_value = {}
        guard.client.get_order.side_effect = [
            {"order": {"order_id": "ord-1", "fill_count": 6, "initial_count": 50}},
            {"order": {"order_id": "ord-2", "fill_count": 0, "initial_count": 50}},
        ]

        stats = guard.run_cycle()

        assert stats["orders_cancelled"] == 2
        assert stats["filled_contracts"] == 6

        # Verify get_order was called for each order after batch cancel
        assert guard.client.get_order.call_count == 2

        # Verify DB got filled_quantity
        db_calls = guard.db.update_order_status.call_args_list
        assert db_calls[0] == (("ord-1", "cancelled"), {"filled_quantity": 6})
        assert db_calls[1] == (("ord-2", "cancelled"), {"filled_quantity": 0})

    def test_individual_cancel_uses_response_fill_count(self):
        """Individual cancel uses fill_count from the cancel response directly."""
        guard = make_guard()
        now = datetime.now(timezone.utc)

        guard.client.get_orders.return_value = {
            "orders": [
                {"order_id": "ord-1", "ticker": "MKT-A", "initial_count": 50},
            ],
            "cursor": "",
        }

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-IND",
                "title": "Individual Cancel Event",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }
        }

        guard.event_cache.put("EVT-IND", {
            "estimated_start_utc": now + timedelta(minutes=1),
            "confidence": "high",
        })

        guard.client.batch_cancel_orders.side_effect = Exception("batch failed")
        guard.client.cancel_order.return_value = {
            "order": {"order_id": "ord-1", "fill_count": 12, "initial_count": 50}
        }

        stats = guard.run_cycle()

        assert stats["orders_cancelled"] == 1
        assert stats["filled_contracts"] == 12

        # Should NOT call get_order since we got fill data from cancel response
        guard.client.get_order.assert_not_called()

    def test_dry_run_no_fill_tracking(self):
        """Dry run reports zero filled contracts."""
        guard = make_guard(dry_run=True)
        now = datetime.now(timezone.utc)

        guard.client.get_orders.return_value = {
            "orders": [
                {"order_id": "ord-1", "ticker": "MKT-A", "initial_count": 50},
            ],
            "cursor": "",
        }

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-DRY",
                "title": "Dry Run Event",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }
        }

        guard.event_cache.put("EVT-DRY", {
            "estimated_start_utc": now + timedelta(minutes=1),
            "confidence": "high",
        })

        stats = guard.run_cycle()

        assert stats["orders_cancelled"] == 1
        assert stats["filled_contracts"] == 0
        guard.client.get_order.assert_not_called()

    def test_get_order_failure_returns_none_fill(self):
        """If get_order fails after batch cancel, filled_quantity is None (unknown)."""
        guard = make_guard()
        now = datetime.now(timezone.utc)

        guard.client.get_orders.return_value = {
            "orders": [
                {"order_id": "ord-1", "ticker": "MKT-A", "initial_count": 50},
            ],
            "cursor": "",
        }

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-GETERR",
                "title": "Get Order Error",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }
        }

        guard.event_cache.put("EVT-GETERR", {
            "estimated_start_utc": now + timedelta(minutes=1),
            "confidence": "high",
        })

        guard.client.batch_cancel_orders.return_value = {}
        guard.client.get_order.side_effect = Exception("API error")

        stats = guard.run_cycle()

        assert stats["orders_cancelled"] == 1
        assert stats["filled_contracts"] == 0
        # Should record None (unknown), NOT 0, so order stays in settlement queue
        guard.db.update_order_status.assert_called_once_with(
            "ord-1", "cancelled", filled_quantity=None
        )


class TestWarmCache:
    @patch("src.order_guard.http_requests.post")
    def test_warm_cache_queries_uncached_events(self, mock_post):
        """warm_cache fetches markets, groups by event, queries Grok for uncached."""
        guard = make_guard()
        now = datetime.now(timezone.utc)
        start_far = now + timedelta(hours=10)

        guard.client.get_market.side_effect = [
            {"market": {
                "event_ticker": "EVT-1",
                "title": "Will announcer say airball?",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }},
            {"market": {
                "event_ticker": "EVT-1",
                "title": "Will announcer say dagger?",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }},
        ]

        guard.client.get_event.return_value = {
            "event": {
                "title": "Lakers vs Rockets NBA Mentions",
                "subtitle": "",
                "category": "NBA",
            }
        }

        mock_post.return_value = _mock_grok_post(_make_grok_response([{
            "event_ticker": "EVT-1",
            "estimated_start_utc": start_far.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "confidence": "high",
            "reasoning": "NBA game tonight",
        }]))

        stats = guard.warm_cache(["MKT-A", "MKT-B"])

        assert stats["tickers"] == 2
        assert stats["events_found"] == 1
        assert stats["events_cached"] == 0
        assert stats["events_queried"] == 1
        # Grok was called
        mock_post.assert_called_once()
        # Event is now cached
        assert guard.event_cache.get("EVT-1") is not None

    def test_warm_cache_skips_cached_events(self):
        """warm_cache doesn't query Grok for events already in the cache."""
        guard = make_guard()
        now = datetime.now(timezone.utc)

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-CACHED",
                "title": "Already Cached Market",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }
        }

        guard.event_cache.put("EVT-CACHED", {
            "estimated_start_utc": now + timedelta(hours=5),
            "confidence": "high",
        })

        stats = guard.warm_cache(["MKT-A"])

        assert stats["tickers"] == 1
        assert stats["events_found"] == 1
        assert stats["events_cached"] == 1
        assert stats["events_queried"] == 0

    def test_warm_cache_handles_market_error(self):
        """If one ticker fails to fetch, others still proceed."""
        guard = make_guard()
        now = datetime.now(timezone.utc)

        guard.client.get_market.side_effect = [
            Exception("API error"),
            {"market": {
                "event_ticker": "EVT-OK",
                "title": "Working Market",
                "close_time": (now + timedelta(hours=3)).isoformat(),
            }},
        ]

        # Pre-cache so we don't need Grok
        guard.event_cache.put("EVT-OK", {
            "estimated_start_utc": now + timedelta(hours=5),
            "confidence": "high",
        })

        stats = guard.warm_cache(["MKT-FAIL", "MKT-OK"])

        assert stats["tickers"] == 2
        assert stats["events_found"] == 1
        assert stats["errors"] == 1


class TestStrategyFiltering:
    def test_guard_with_strategies_queries_db_per_strategy(self):
        """Guard with strategies=['mentions'] only queries DB for mentions orders."""
        guard = make_guard(strategies=["mentions"], pending_order_ids=set())

        # Override the mock to return different sets per strategy
        def side_effect(strategy=None):
            if strategy == "mentions":
                return {"ord-m1", "ord-m2"}
            return set()

        guard.db.get_pending_order_ids.side_effect = side_effect
        guard.client.get_orders.return_value = {
            "orders": [
                {"order_id": "ord-m1", "ticker": "MKT-A"},
                {"order_id": "ord-other", "ticker": "MKT-B"},
            ],
            "cursor": "",
        }

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-1",
                "title": "Test",
                "close_time": (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat(),
            }
        }

        guard.event_cache.put("EVT-1", {
            "estimated_start_utc": datetime.now(timezone.utc) + timedelta(hours=10),
            "confidence": "high",
        })

        stats = guard.run_cycle()

        # Should only find ord-m1 (the mentions order that's resting)
        assert stats["resting_orders"] == 1
        # DB should have been queried specifically for mentions
        guard.db.get_pending_order_ids.assert_called_once_with(strategy="mentions")

    def test_guard_without_strategies_queries_all(self):
        """Guard without strategies queries DB with strategy=None."""
        guard = make_guard(strategies=None)
        guard.db.get_pending_order_ids.return_value = set()

        guard.run_cycle()

        guard.db.get_pending_order_ids.assert_called_once_with(strategy=None)

    def test_guard_multiple_strategies_unions_order_ids(self):
        """Guard with multiple strategies unions order IDs from each."""
        guard = make_guard(strategies=["mentions", "longshot"], pending_order_ids=set())

        call_count = {"n": 0}
        def side_effect(strategy=None):
            call_count["n"] += 1
            if strategy == "mentions":
                return {"ord-m1"}
            elif strategy == "longshot":
                return {"ord-l1"}
            return set()

        guard.db.get_pending_order_ids.side_effect = side_effect
        guard.client.get_orders.return_value = {
            "orders": [
                {"order_id": "ord-m1", "ticker": "MKT-A"},
                {"order_id": "ord-l1", "ticker": "MKT-B"},
                {"order_id": "ord-other", "ticker": "MKT-C"},
            ],
            "cursor": "",
        }

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-1",
                "title": "Test",
                "close_time": (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat(),
            }
        }

        guard.event_cache.put("EVT-1", {
            "estimated_start_utc": datetime.now(timezone.utc) + timedelta(hours=10),
            "confidence": "high",
        })

        stats = guard.run_cycle()

        assert stats["resting_orders"] == 2
        assert guard.db.get_pending_order_ids.call_count == 2


class TestMilestones:
    def test_milestone_resolves_start_time(self):
        """Milestone with start_date caches as high confidence; Grok NOT called."""
        guard = make_guard()
        now = datetime.now(timezone.utc)
        start = now + timedelta(hours=3)

        guard.client.get_milestones.return_value = {
            "milestones": [{
                "start_date": start.isoformat(),
                "category": "mentions",
            }]
        }
        guard.client.get_event.return_value = {
            "event": {"title": "Lakers vs Rockets NBA Mentions"}
        }

        guard.client.get_orders.return_value = {
            "orders": [{"order_id": "ord-1", "ticker": "MKT-A"}],
            "cursor": "",
        }
        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-MS",
                "title": "Will announcer say airball?",
                "close_time": (now + timedelta(hours=5)).isoformat(),
            }
        }

        with patch("src.order_guard.http_requests.post") as mock_post:
            stats = guard.run_cycle()
            mock_post.assert_not_called()

        cached = guard.event_cache.get("EVT-MS")
        assert cached is not None
        assert cached["confidence"] == "high"
        assert cached["reasoning"] == "Kalshi milestones API"

    def test_milestone_fallback_to_grok(self):
        """Empty milestones response falls back to Grok."""
        guard = make_guard()
        now = datetime.now(timezone.utc)
        start_far = now + timedelta(hours=10)

        # Milestones returns empty
        guard.client.get_milestones.return_value = {"milestones": []}

        guard.client.get_orders.return_value = {
            "orders": [{"order_id": "ord-1", "ticker": "MKT-A"}],
            "cursor": "",
        }
        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-FB",
                "title": "Test market",
                "close_time": (now + timedelta(hours=5)).isoformat(),
            }
        }
        guard.client.get_event.return_value = {
            "event": {"title": "Test Event", "subtitle": "", "category": ""}
        }

        with patch("src.order_guard.http_requests.post") as mock_post:
            mock_post.return_value = _mock_grok_post(_make_grok_response([{
                "event_ticker": "EVT-FB",
                "estimated_start_utc": start_far.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "confidence": "medium",
                "reasoning": "Grok estimate",
            }]))

            guard.run_cycle()

            # Grok WAS called since milestones returned nothing
            mock_post.assert_called_once()

        cached = guard.event_cache.get("EVT-FB")
        assert cached is not None
        assert cached["confidence"] == "medium"

    def test_milestone_api_error_falls_back(self):
        """If get_milestones raises, falls back to Grok."""
        guard = make_guard()
        now = datetime.now(timezone.utc)
        start_far = now + timedelta(hours=10)

        guard.client.get_milestones.side_effect = Exception("API error")

        guard.client.get_orders.return_value = {
            "orders": [{"order_id": "ord-1", "ticker": "MKT-A"}],
            "cursor": "",
        }
        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-ERR",
                "title": "Test market",
                "close_time": (now + timedelta(hours=5)).isoformat(),
            }
        }
        guard.client.get_event.return_value = {
            "event": {"title": "Test Event", "subtitle": "", "category": ""}
        }

        with patch("src.order_guard.http_requests.post") as mock_post:
            mock_post.return_value = _mock_grok_post(_make_grok_response([{
                "event_ticker": "EVT-ERR",
                "estimated_start_utc": start_far.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "confidence": "high",
                "reasoning": "Grok estimate",
            }]))

            guard.run_cycle()

            mock_post.assert_called_once()

    def test_milestone_no_start_date_falls_back(self):
        """Milestone exists but has no start_date field — falls back to Grok."""
        guard = make_guard()
        now = datetime.now(timezone.utc)
        start_far = now + timedelta(hours=10)

        guard.client.get_milestones.return_value = {
            "milestones": [{"category": "mentions", "title": "Game"}]
        }

        guard.client.get_orders.return_value = {
            "orders": [{"order_id": "ord-1", "ticker": "MKT-A"}],
            "cursor": "",
        }
        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-NODT",
                "title": "Test market",
                "close_time": (now + timedelta(hours=5)).isoformat(),
            }
        }
        guard.client.get_event.return_value = {
            "event": {"title": "Test Event", "subtitle": "", "category": ""}
        }

        with patch("src.order_guard.http_requests.post") as mock_post:
            mock_post.return_value = _mock_grok_post(_make_grok_response([{
                "event_ticker": "EVT-NODT",
                "estimated_start_utc": start_far.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "confidence": "medium",
                "reasoning": "Grok fallback",
            }]))

            guard.run_cycle()

            mock_post.assert_called_once()

    def test_milestone_mixed_results(self):
        """3 events: 2 resolved by milestones, 1 falls back to Grok."""
        guard = make_guard()
        now = datetime.now(timezone.utc)
        start = now + timedelta(hours=6)

        def milestones_side_effect(ticker):
            if ticker == "EVT-A":
                return {"milestones": [{"start_date": start.isoformat(), "category": "mentions"}]}
            elif ticker == "EVT-B":
                return {"milestones": [{"start_date": (start + timedelta(hours=1)).isoformat()}]}
            else:
                return {"milestones": []}

        guard.client.get_milestones.side_effect = milestones_side_effect
        guard.client.get_event.return_value = {
            "event": {"title": "Test Event", "subtitle": "", "category": ""}
        }

        guard.client.get_orders.return_value = {
            "orders": [
                {"order_id": "ord-1", "ticker": "MKT-A"},
                {"order_id": "ord-2", "ticker": "MKT-B"},
                {"order_id": "ord-3", "ticker": "MKT-C"},
            ],
            "cursor": "",
        }

        guard.client.get_market.side_effect = [
            {"market": {"event_ticker": "EVT-A", "title": "M1", "close_time": (now + timedelta(hours=8)).isoformat()}},
            {"market": {"event_ticker": "EVT-B", "title": "M2", "close_time": (now + timedelta(hours=8)).isoformat()}},
            {"market": {"event_ticker": "EVT-C", "title": "M3", "close_time": (now + timedelta(hours=8)).isoformat()}},
        ]

        with patch("src.order_guard.http_requests.post") as mock_post:
            mock_post.return_value = _mock_grok_post(_make_grok_response([{
                "event_ticker": "EVT-C",
                "estimated_start_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "confidence": "medium",
                "reasoning": "Grok for EVT-C",
            }]))

            stats = guard.run_cycle()

            # Grok called once (for EVT-C only)
            mock_post.assert_called_once()

        # All 3 should be cached
        assert guard.event_cache.get("EVT-A") is not None
        assert guard.event_cache.get("EVT-A")["confidence"] == "high"
        assert guard.event_cache.get("EVT-B") is not None
        assert guard.event_cache.get("EVT-B")["confidence"] == "high"
        assert guard.event_cache.get("EVT-C") is not None
        assert guard.event_cache.get("EVT-C")["confidence"] == "medium"

    @patch("src.order_guard.http_requests.post")
    def test_warm_cache_uses_milestones(self, mock_post):
        """warm_cache tries milestones before Grok and tracks stats."""
        guard = make_guard()
        now = datetime.now(timezone.utc)
        start = now + timedelta(hours=5)

        guard.client.get_milestones.return_value = {
            "milestones": [{"start_date": start.isoformat()}]
        }
        guard.client.get_event.return_value = {
            "event": {"title": "Test Event"}
        }

        guard.client.get_market.return_value = {
            "market": {
                "event_ticker": "EVT-WC",
                "title": "Warm cache market",
                "close_time": (now + timedelta(hours=8)).isoformat(),
            }
        }

        stats = guard.warm_cache(["MKT-A"])

        assert stats["milestones_resolved"] == 1
        assert stats["grok_queried"] == 0
        mock_post.assert_not_called()

        cached = guard.event_cache.get("EVT-WC")
        assert cached is not None
        assert cached["confidence"] == "high"


class TestMilestoneRefresh:
    def test_refresh_updates_changed_start_time(self):
        """Milestones returns a different time -> cache updated."""
        guard = make_guard()
        now = datetime.now(timezone.utc)
        old_start = now + timedelta(hours=3)
        new_start = now + timedelta(hours=5)

        # Pre-cache with old time, mark milestones_checked_at as stale
        guard.event_cache.put("EVT-A", {
            "estimated_start_utc": old_start,
            "confidence": "high",
            "reasoning": "Kalshi milestones API",
            "event_title": "Lakers Game",
            "milestones_checked_at": now - timedelta(hours=1),
        })

        # Milestones returns updated time
        guard.client.get_milestones.return_value = {
            "milestones": [{"start_date": new_start.isoformat(), "category": "mentions"}]
        }

        updated = guard._refresh_milestones()

        assert updated == 1
        cached = guard.event_cache.get("EVT-A")
        assert cached["estimated_start_utc"] == new_start
        assert cached["confidence"] == "high"
        assert "refreshed" in cached["reasoning"]
        assert cached["milestones_checked_at"] is not None

    def test_refresh_keeps_existing_on_empty(self):
        """Milestones returns empty -> cache unchanged, checked_at updated."""
        guard = make_guard()
        now = datetime.now(timezone.utc)
        original_start = now + timedelta(hours=3)

        guard.event_cache.put("EVT-B", {
            "estimated_start_utc": original_start,
            "confidence": "medium",
            "reasoning": "Grok estimate",
            "event_title": "Some Game",
            "milestones_checked_at": now - timedelta(hours=1),
        })

        guard.client.get_milestones.return_value = {"milestones": []}

        updated = guard._refresh_milestones()

        assert updated == 0
        cached = guard.event_cache.get("EVT-B")
        # Start time unchanged
        assert cached["estimated_start_utc"] == original_start
        assert cached["confidence"] == "medium"
        # But milestones_checked_at was updated (no longer stale)
        assert cached["milestones_checked_at"] > now - timedelta(seconds=5)

    def test_refresh_keeps_existing_on_api_error(self):
        """Milestones API throws -> cache unchanged, checked_at updated."""
        guard = make_guard()
        now = datetime.now(timezone.utc)
        original_start = now + timedelta(hours=3)

        guard.event_cache.put("EVT-C", {
            "estimated_start_utc": original_start,
            "confidence": "high",
            "reasoning": "Kalshi milestones API",
            "event_title": "Test Event",
            "milestones_checked_at": now - timedelta(hours=1),
        })

        guard.client.get_milestones.side_effect = Exception("API down")

        updated = guard._refresh_milestones()

        assert updated == 0
        cached = guard.event_cache.get("EVT-C")
        # Start time unchanged
        assert cached["estimated_start_utc"] == original_start
        # milestones_checked_at updated so we don't retry every cycle
        assert cached["milestones_checked_at"] > now - timedelta(seconds=5)

    def test_refresh_skips_recently_checked(self):
        """Events checked <30min ago are not re-queried."""
        guard = make_guard()
        now = datetime.now(timezone.utc)

        guard.event_cache.put("EVT-D", {
            "estimated_start_utc": now + timedelta(hours=3),
            "confidence": "high",
            "reasoning": "Kalshi milestones API",
            "event_title": "Recent Event",
            "milestones_checked_at": now - timedelta(minutes=10),  # Only 10min ago
        })

        updated = guard._refresh_milestones()

        assert updated == 0
        # Milestones API should NOT have been called
        guard.client.get_milestones.assert_not_called()

    def test_refresh_runs_in_cycle(self):
        """run_cycle calls _refresh_milestones and includes stat."""
        guard = make_guard()
        now = datetime.now(timezone.utc)

        # Pre-cache an event with stale milestones_checked_at
        guard.event_cache.put("EVT-E", {
            "estimated_start_utc": now + timedelta(hours=3),
            "confidence": "high",
            "reasoning": "Kalshi milestones API",
            "event_title": "Test Event",
            "milestones_checked_at": now - timedelta(hours=1),
        })

        new_start = now + timedelta(hours=4)
        guard.client.get_milestones.return_value = {
            "milestones": [{"start_date": new_start.isoformat(), "category": "mentions"}]
        }

        # No resting orders
        guard.client.get_orders.return_value = {"orders": [], "cursor": ""}

        stats = guard.run_cycle()

        assert stats["milestones_refreshed"] == 1
        # Verify cache was updated
        cached = guard.event_cache.get("EVT-E")
        assert cached["estimated_start_utc"] == new_start

    def test_refresh_same_time_no_update_count(self):
        """When milestones returns the same time, updated count stays 0."""
        guard = make_guard()
        now = datetime.now(timezone.utc)
        start = now + timedelta(hours=3)

        guard.event_cache.put("EVT-F", {
            "estimated_start_utc": start,
            "confidence": "high",
            "reasoning": "Kalshi milestones API",
            "event_title": "Same Time Event",
            "milestones_checked_at": now - timedelta(hours=1),
        })

        guard.client.get_milestones.return_value = {
            "milestones": [{"start_date": start.isoformat(), "category": "mentions"}]
        }

        updated = guard._refresh_milestones()

        assert updated == 0
        # But milestones_checked_at was refreshed
        cached = guard.event_cache.get("EVT-F")
        assert cached["milestones_checked_at"] > now - timedelta(seconds=5)

    def test_refresh_caps_api_calls_per_cycle(self):
        """Only max_per_cycle events are checked per refresh to avoid API burst."""
        guard = make_guard()
        now = datetime.now(timezone.utc)

        # Cache 8 stale events
        for i in range(8):
            guard.event_cache.put(f"EVT-{i}", {
                "estimated_start_utc": now + timedelta(hours=3),
                "confidence": "high",
                "reasoning": "Kalshi milestones API",
                "event_title": f"Event {i}",
                "milestones_checked_at": now - timedelta(hours=1),
            })

        guard.client.get_milestones.return_value = {"milestones": []}

        guard._refresh_milestones(max_per_cycle=3)

        # Only 3 API calls despite 8 stale events
        assert guard.client.get_milestones.call_count == 3


class TestEventCacheStaleMilestones:
    def test_get_stale_milestones_returns_unchecked(self):
        """Events with milestones_checked_at=None are returned as stale."""
        cache = EventCache(_make_mock_db())
        now = datetime.now(timezone.utc)

        cache.put("EVT-1", {
            "estimated_start_utc": now + timedelta(hours=3),
            "confidence": "high",
        })
        # milestones_checked_at defaults to None via put()

        stale = cache.get_stale_milestones(max_age_minutes=30)
        assert "EVT-1" in stale

    def test_get_stale_milestones_skips_recent(self):
        """Events checked recently are not returned."""
        cache = EventCache(_make_mock_db())
        now = datetime.now(timezone.utc)

        cache.put("EVT-1", {
            "estimated_start_utc": now + timedelta(hours=3),
            "confidence": "high",
            "milestones_checked_at": now - timedelta(minutes=5),
        })

        stale = cache.get_stale_milestones(max_age_minutes=30)
        assert "EVT-1" not in stale

    def test_get_stale_milestones_returns_old(self):
        """Events checked >30min ago are returned."""
        cache = EventCache(_make_mock_db())
        now = datetime.now(timezone.utc)

        cache.put("EVT-1", {
            "estimated_start_utc": now + timedelta(hours=3),
            "confidence": "high",
            "milestones_checked_at": now - timedelta(minutes=45),
        })

        stale = cache.get_stale_milestones(max_age_minutes=30)
        assert "EVT-1" in stale

    def test_db_loaded_entries_have_null_milestones_checked(self):
        """Entries loaded from DB start with milestones_checked_at=None."""
        now = datetime.now(timezone.utc)
        start = now + timedelta(hours=3)
        db = _make_mock_db(rows=[{
            "event_ticker": "EVT-DB",
            "estimated_start_utc": start.isoformat(),
            "confidence": "high",
            "reasoning": "from db",
            "cached_at": now.isoformat(),
        }])

        cache = EventCache(db)

        entry = cache.get("EVT-DB")
        assert entry is not None
        assert entry["milestones_checked_at"] is None
        # So it shows up as stale
        stale = cache.get_stale_milestones(max_age_minutes=30)
        assert "EVT-DB" in stale
