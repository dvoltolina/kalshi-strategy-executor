# src/logging_config.py
import logging
import os
import sys
from datetime import datetime
from pathlib import Path


def setup_logging(log_level: str = "INFO", log_dir: str = "logs") -> str:
    """Configure logging to console and file.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_dir: Directory for log files

    Returns:
        Path to the log file
    """
    # Create log directory if needed
    log_path = Path(log_dir)
    log_path.mkdir(exist_ok=True)

    # Generate log filename with timestamp
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_file = log_path / f"bot_{timestamp}.log"

    # Get numeric log level
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    # Create formatters
    file_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    # Console shows timestamp for better debugging
    console_formatter = logging.Formatter("%(asctime)s | %(message)s", datefmt="%H:%M:%S")

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Remove existing handlers
    root_logger.handlers.clear()

    # File handler - detailed logging
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)  # Always log everything to file
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)

    # Console handler - summary only
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    return str(log_file)
