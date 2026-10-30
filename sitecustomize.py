"""Early runtime diagnostics for BotHost and the embedded cashier.

Python imports sitecustomize automatically when this file is on sys.path.
It installs bounded file logging before bot.py/cashier.py initialize logging.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

def _positive_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default

def _redact(value: object) -> str:
    text = str(value)
    # Avoid leaking Telegram/Groq/API credentials if an exception includes one.
    text = re.sub(r"(?i)(bot_token|cashier_token|groq_api_key|sync_secret|api[_-]?key)(\s*[=:]\s*)[^\s,;]+",
                  r"\1\2[REDACTED]", text)
    text = re.sub(r"\b\d{8,}:[A-Za-z0-9_-]{20,}\b", "[TELEGRAM_TOKEN_REDACTED]", text)
    return text

class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = _redact(record.getMessage())
            record.args = ()
        except Exception:
            record.msg = "[log message redacted: formatting error]"
            record.args = ()
        return True

def _install_logging() -> None:
    root = logging.getLogger()
    if getattr(root, "_bothost_diagnostics_installed", False):
        return

    data_dir = Path(os.getenv("DATA_DIR", "/app/data"))
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        log_path = data_dir / "bothost-runtime.log"
        # Default 10 MiB per file, keep 3 old files. Can be changed in Railway.
        max_bytes = _positive_int("MAX_HOST_LOG_MB", 10) * 1024 * 1024
        file_handler = RotatingFileHandler(
            log_path, maxBytes=max_bytes, backupCount=3, encoding="utf-8"
        )
    except Exception:
        # Logging must never prevent the bot from starting if the Volume is unavailable.
        log_path = Path("/tmp/bothost-runtime.log")
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=_positive_int("MAX_HOST_LOG_MB", 10) * 1024 * 1024,
            backupCount=2,
            encoding="utf-8",
        )

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream_handler = logging.StreamHandler(sys.stdout)
    for handler in (file_handler, stream_handler):
        handler.setFormatter(formatter)
        handler.addFilter(_RedactingFilter())

    # Configure root before bot.py calls basicConfig; all module loggers inherit it.
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    root.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))
    root._bothost_diagnostics_installed = True

    logging.getLogger("BotHost.Diagnostics").info(
        "Diagnostics initialized | python=%s | pid=%s | log_file=%s | max_file_mb=%s",
        sys.version.split()[0], os.getpid(), log_path,
        _positive_int("MAX_HOST_LOG_MB", 10),
    )

    previous_excepthook = sys.excepthook
    def _main_exception_hook(exc_type, exc_value, exc_tb):
        logging.getLogger("BotHost.Crash").critical(
            "Uncaught main-thread exception", exc_info=(exc_type, exc_value, exc_tb)
        )
        previous_excepthook(exc_type, exc_value, exc_tb)
    sys.excepthook = _main_exception_hook

    if hasattr(threading, "excepthook"):
        def _thread_exception_hook(args):
            logging.getLogger("BotHost.Crash").critical(
                "Uncaught exception in thread %s",
                getattr(args.thread, "name", "<unknown>"),
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        threading.excepthook = _thread_exception_hook

_install_logging()
