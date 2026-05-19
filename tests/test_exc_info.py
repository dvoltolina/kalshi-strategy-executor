# tests/test_exc_info.py
"""Ensure all broad exception handlers log with exc_info=True.

This prevents silent exception swallowing — the bug pattern where
logger.error(f"...{e}") loses the full traceback.
"""
import ast
import os
from pathlib import Path

SRC_DIR = Path(__file__).parent.parent / "src"

# Files where we enforce exc_info=True on all except-block logger calls
ENFORCED_FILES = [
    "runner.py",
    "order_guard.py",
    "order_placer.py",
    "ws_client.py",
    "settlement_checker.py",
    "reconciler.py",
    "kalshi_client.py",
    "main.py",
    "strategies/__init__.py",
]


def _find_except_loggers_missing_exc_info(filepath: Path) -> list:
    """Find logger.error/warning calls inside except blocks missing exc_info=True."""
    source = filepath.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(filepath))

    violations = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue

        # Walk all calls inside this except block
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue

            # Match logger.error(...) or logger.warning(...)
            func = child.func
            if not (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "logger"
                and func.attr in ("error", "warning")
            ):
                continue

            # Check if exc_info=True is present in keyword args
            has_exc_info = any(
                kw.arg == "exc_info"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in child.keywords
            )

            if not has_exc_info:
                violations.append(
                    f"{filepath.relative_to(SRC_DIR.parent)}:{child.lineno} "
                    f"logger.{func.attr}() missing exc_info=True"
                )

    return violations


def test_all_except_handlers_have_exc_info():
    """Every logger.error/warning inside an except block must have exc_info=True."""
    all_violations = []

    for relpath in ENFORCED_FILES:
        filepath = SRC_DIR / relpath
        assert filepath.exists(), f"Enforced file not found: {filepath}"
        all_violations.extend(_find_except_loggers_missing_exc_info(filepath))

    if all_violations:
        msg = (
            f"Found {len(all_violations)} logger calls in except blocks "
            f"missing exc_info=True:\n" + "\n".join(f"  - {v}" for v in all_violations)
        )
        raise AssertionError(msg)
