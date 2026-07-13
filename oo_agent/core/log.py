"""Logging setup: stderr handler, plain single-line format."""

from __future__ import annotations

import logging


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Noisy third-party loggers stay at WARNING regardless of our level.
    for noisy in ("urllib3", "httpx", "docker"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
