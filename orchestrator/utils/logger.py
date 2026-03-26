"""
Structured logging with per-run file sinks and a rich console handler.
"""

import logging
import sys
from pathlib import Path
from typing import Optional

from rich.logging import RichHandler


def setup_logger(
    name: str = "orchestrator",
    run_id: str = "default",
    log_dir: str = "logs",
    verbose: bool = False,
) -> logging.Logger:
    """
    Configure and return a logger that writes to both the console (via Rich)
    and a per-run log file under `log_dir/`.

    Args:
        name:     Logger name (usually the module name).
        run_id:   Unique identifier for this orchestration run.
        log_dir:  Directory where log files are stored.
        verbose:  When True, set console level to DEBUG; otherwise INFO.

    Returns:
        A configured Logger instance.
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)  # capture everything; handlers filter

    # Avoid adding duplicate handlers when called multiple times
    if logger.handlers:
        return logger

    console_level = logging.DEBUG if verbose else logging.INFO

    # --- Rich console handler ---------------------------------------------------
    console_handler = RichHandler(
        level=console_level,
        show_time=True,
        show_path=False,
        markup=True,
        rich_tracebacks=True,
    )
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console_handler)

    # --- File handler (always DEBUG so we capture everything) -------------------
    file_handler = logging.FileHandler(log_path / f"{run_id}.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    logger.addHandler(file_handler)

    return logger


def get_logger(name: str) -> logging.Logger:
    """Return an existing logger by name (no new handlers added)."""
    return logging.getLogger(name)
