# tests/test_state_manager.py
import json
import pytest
from pathlib import Path


@pytest.fixture
def state_dir(tmp_path):
    """Create a temporary state directory."""
    return tmp_path / "state"


@pytest.fixture
def state_manager(state_dir):
    """Create a StateManager with a temporary directory."""
    from src.state_manager import StateManager
    return StateManager(state_dir)


def test_save_and_load_event_cache(state_manager):
    """Event cache can be saved and loaded."""
    cache = {
        "EVENT-1": {"category": "NFL", "title": "Super Bowl"},
        "EVENT-2": {"category": "NBA", "title": "Finals"},
    }

    state_manager.save_event_cache(cache)
    loaded = state_manager.load_event_cache()

    assert loaded == cache


def test_load_event_cache_empty_when_no_file(state_manager):
    """Loading event cache returns empty dict when file doesn't exist."""
    loaded = state_manager.load_event_cache()
    assert loaded == {}


def test_load_event_cache_handles_corrupted_file(state_manager, state_dir):
    """Loading event cache returns empty dict on corrupted JSON."""
    state_dir.mkdir(parents=True, exist_ok=True)
    cache_file = state_dir / "event_cache.json"
    cache_file.write_text("not valid json")

    loaded = state_manager.load_event_cache()
    assert loaded == {}


def test_append_qualifying_market(state_manager):
    """Markets can be appended incrementally."""
    market1 = {"ticker": "NFL-GAME-1", "category": "nfl"}
    market2 = {"ticker": "NBA-GAME-1", "category": "nba"}

    state_manager.append_qualifying_market(market1)
    state_manager.append_qualifying_market(market2)

    loaded = state_manager.load_qualifying_markets()
    assert len(loaded) == 2
    assert loaded[0]["ticker"] == "NFL-GAME-1"
    assert loaded[1]["ticker"] == "NBA-GAME-1"


def test_load_qualifying_markets_empty_when_no_file(state_manager):
    """Loading qualifying markets returns empty list when file doesn't exist."""
    loaded = state_manager.load_qualifying_markets()
    assert loaded == []


def test_save_and_load_progress(state_manager):
    """Progress can be saved and loaded."""
    state_manager.save_progress(processed=50, total=200)

    progress = state_manager.load_progress()
    assert progress["processed"] == 50
    assert progress["total_markets"] == 200
    assert "timestamp" in progress


def test_load_progress_returns_none_when_no_file(state_manager):
    """Loading progress returns None when file doesn't exist."""
    progress = state_manager.load_progress()
    assert progress is None


def test_has_resume_state(state_manager):
    """has_resume_state returns True only when progress file exists."""
    assert not state_manager.has_resume_state()

    state_manager.save_progress(10, 100)

    assert state_manager.has_resume_state()


def test_clear_state(state_manager):
    """clear_state removes all state files."""
    # Create state
    state_manager.save_event_cache({"EVENT-1": {}})
    state_manager.append_qualifying_market({"ticker": "M1"})
    state_manager.save_progress(50, 100)

    # Verify files exist
    assert state_manager.event_cache_file.exists()
    assert state_manager.markets_file.exists()
    assert state_manager.progress_file.exists()

    # Clear state
    state_manager.clear_state()

    # Verify files are gone
    assert not state_manager.event_cache_file.exists()
    assert not state_manager.markets_file.exists()
    assert not state_manager.progress_file.exists()


def test_clear_state_when_no_files_exist(state_manager):
    """clear_state doesn't fail when files don't exist."""
    # Should not raise
    state_manager.clear_state()


def test_ensure_state_dir_creates_directory(state_manager, state_dir):
    """ensure_state_dir creates the directory if it doesn't exist."""
    assert not state_dir.exists()

    state_manager.ensure_state_dir()

    assert state_dir.exists()


def test_ensure_state_dir_is_idempotent(state_manager, state_dir):
    """ensure_state_dir can be called multiple times."""
    state_manager.ensure_state_dir()
    state_manager.ensure_state_dir()

    assert state_dir.exists()
