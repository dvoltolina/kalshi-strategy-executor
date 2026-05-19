# src/state_manager.py
"""State persistence for resumable scans."""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class StateManager:
    """Manages persistent state for resumable market scans."""

    def __init__(self, state_dir: Path = Path("state")):
        self.state_dir = state_dir
        self.event_cache_file = state_dir / "event_cache.json"
        self.markets_file = state_dir / "qualifying_markets.json"
        self.progress_file = state_dir / "progress.json"

    def ensure_state_dir(self) -> None:
        """Create state directory if it doesn't exist."""
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def has_resume_state(self) -> bool:
        """Check if there's existing state to resume from."""
        return self.progress_file.exists()

    def load_event_cache(self) -> Dict[str, Any]:
        """Load cached event data from disk."""
        if not self.event_cache_file.exists():
            return {}
        try:
            with open(self.event_cache_file, "r") as f:
                cache = json.load(f)
            logger.debug(f"Loaded {len(cache)} cached events from disk")
            return cache
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Failed to load event cache: {e}")
            return {}

    def save_event_cache(self, cache: Dict[str, Any]) -> None:
        """Save event cache to disk."""
        self.ensure_state_dir()
        try:
            with open(self.event_cache_file, "w") as f:
                json.dump(cache, f, indent=2)
        except IOError as e:
            logger.warning(f"Failed to save event cache: {e}")

    def load_qualifying_markets(self) -> List[Dict[str, Any]]:
        """Load qualifying markets found so far."""
        if not self.markets_file.exists():
            return []
        try:
            with open(self.markets_file, "r") as f:
                markets = json.load(f)
            logger.debug(f"Loaded {len(markets)} qualifying markets from disk")
            return markets
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Failed to load qualifying markets: {e}")
            return []

    def append_qualifying_market(self, market: Dict[str, Any]) -> None:
        """Append a single qualifying market to the file."""
        self.ensure_state_dir()
        markets = self.load_qualifying_markets()
        markets.append(market)
        try:
            with open(self.markets_file, "w") as f:
                json.dump(markets, f, indent=2)
        except IOError as e:
            logger.warning(f"Failed to save qualifying market: {e}")

    def save_progress(self, processed: int, total: int) -> None:
        """Save scan progress."""
        self.ensure_state_dir()
        progress = {
            "processed": processed,
            "total_markets": total,
            "timestamp": datetime.now().isoformat(),
        }
        try:
            with open(self.progress_file, "w") as f:
                json.dump(progress, f, indent=2)
        except IOError as e:
            logger.warning(f"Failed to save progress: {e}")

    def load_progress(self) -> Optional[Dict[str, Any]]:
        """Load previous scan progress."""
        if not self.progress_file.exists():
            return None
        try:
            with open(self.progress_file, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Failed to load progress: {e}")
            return None

    def clear_state(self) -> None:
        """Clear all state files (called on successful completion)."""
        for file in [self.event_cache_file, self.markets_file, self.progress_file]:
            if file.exists():
                try:
                    file.unlink()
                    logger.debug(f"Removed state file: {file}")
                except IOError as e:
                    logger.warning(f"Failed to remove {file}: {e}")
        # Remove directory if empty
        if self.state_dir.exists():
            try:
                self.state_dir.rmdir()
            except OSError:
                pass  # Directory not empty or other error, ignore
