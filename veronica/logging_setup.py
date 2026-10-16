"""
Centralized logging + diagnostics for VERONICA.

Every module does `logger = logging.getLogger(__name__)` and logs normally;
this module is the ONLY place that configures handlers/formatters, so log
behavior (level, rotation, format) is controlled from one spot instead of
being copy-pasted (or worse, replaced by scattered `print()` calls) across
the codebase.
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_CONFIGURED = False


def setup_logging(log_level: str = "INFO", log_file: str | Path | None = None,
                  console_level: str | None = None) -> None:
    """`console_level` limits what reaches the terminal (the log file always gets
    `log_level`). "CRITICAL" keeps the terminal to the conversation itself."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger("veronica")
    root.setLevel(log_level.upper())
    root.propagate = False

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-28s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    if console_level:
        console.setLevel(console_level.upper())
    root.addHandler(console)

    file_handler = None
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    if console_level and console_level.upper() in ("ERROR", "CRITICAL"):
        # Library warnings (deprecation notices etc.) go to the log file, not the conversation.
        logging.captureWarnings(True)
        warnings_logger = logging.getLogger("py.warnings")
        warnings_logger.propagate = False
        warnings_logger.handlers = [file_handler] if file_handler else [logging.NullHandler()]

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"veronica.{name}")
