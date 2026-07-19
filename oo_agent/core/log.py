"""Logging setup: stderr handler, optional rotating log file."""

from __future__ import annotations

import logging
import logging.handlers
import os

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO", log_file: str = "") -> None:
    """Configure the root logger.

    ``log_file`` adds a size-rotated file (5 MB x 3) next to the always
    present stderr handler, so the reason for a crash survives even on
    hosts where nobody keeps journal history.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)
    if not root.handlers:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)
    if log_file:
        try:
            directory = os.path.dirname(log_file)
            if directory:
                os.makedirs(directory, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=5 * 1024 * 1024, backupCount=3,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError as exc:
            logging.getLogger("log").warning(
                "log file %s unavailable: %s", log_file, exc
            )
    # Noisy third-party loggers stay at WARNING regardless of our level.
    for noisy in ("urllib3", "httpx", "docker"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
