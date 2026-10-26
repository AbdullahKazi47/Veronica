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
import os
import warnings
from pathlib import Path

# Model downloads (Hugging Face / fastembed, used by Mem0) draw progress bars and print
# "Fetching 18 files" / "Download complete" straight into the terminal - the conversation.
# Set before those libraries are imported (they read these once, on import).
_QUIET_ENV = {"HF_HUB_DISABLE_PROGRESS_BARS": "1", "HF_HUB_DISABLE_SYMLINKS_WARNING": "1",
              "HF_HUB_DISABLE_TELEMETRY": "1", "HF_HUB_VERBOSITY": "error", "TQDM_DISABLE": "1",
              "TRANSFORMERS_VERBOSITY": "error", "TOKENIZERS_PARALLELISM": "false"}

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

    global _FILE_HANDLER, _QUIET
    _FILE_HANDLER = file_handler
    if console_level and console_level.upper() in ("ERROR", "CRITICAL"):
        _QUIET = True
        for k, v in _QUIET_ENV.items():
            os.environ.setdefault(k, v)
        quiet_third_party()
        # Library warnings (deprecation notices etc.) go to the log file, not the conversation.
        logging.captureWarnings(True)
        warnings_logger = logging.getLogger("py.warnings")
        warnings_logger.propagate = False
        warnings_logger.handlers = [file_handler] if file_handler else [logging.NullHandler()]
        # Third-party deprecation notices we can't fix, repeated on every mic open / import.
        warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"sounddevice")
        warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"google\.genai")

    _CONFIGURED = True


_FILE_HANDLER: logging.Handler | None = None
_QUIET = False


def quiet_third_party() -> None:
    """Keep other libraries' log lines out of the terminal (the conversation): their
    console handlers are removed and their messages go to the log file only. Libraries
    such as browser-use set up console logging on import, after setup_logging ran, so
    this runs again once everything is loaded."""
    if not _QUIET:
        return
    targets = [logging.getLogger()] + [lg for name, lg in logging.Logger.manager.loggerDict.items()
                                       if isinstance(lg, logging.Logger) and not name.startswith("veronica")]
    for lg in targets:
        for h in list(lg.handlers):
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                lg.removeHandler(h)
    root = logging.getLogger()
    if _FILE_HANDLER is not None and _FILE_HANDLER not in root.handlers:
        root.addHandler(_FILE_HANDLER)
    if not root.handlers:
        root.addHandler(logging.NullHandler())
    root.setLevel(logging.WARNING)
    _loguru_to_file()


def _loguru_to_file() -> None:
    """fastembed logs through loguru, whose default sink is the terminal ("Local file sizes do
    not match the metadata"). Send its warnings to the log file instead."""
    try:
        from loguru import logger as loguru_logger
    except Exception:  # noqa: BLE001 - not installed: nothing to redirect
        return
    try:
        loguru_logger.remove()
        loguru_logger.add(lambda m: logging.getLogger("loguru").warning(str(m).rstrip()), level="WARNING",
                          format="{name}: {message}")
    except Exception:  # noqa: BLE001
        pass


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"veronica.{name}")
