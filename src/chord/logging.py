"""Small, dependency-free logging surface used by Chord's runtime."""

import os
import sys
import time
from typing import TextIO

from chord.text import one_line

DEBUG = 10
INFO = 20
WARNING = 30
ERROR = 40

_LEVELS = {"DEBUG": DEBUG, "INFO": INFO, "WARNING": WARNING, "ERROR": ERROR}


def _configured_level() -> int:
    value = os.environ.get("CHORD_LOG_LEVEL", "INFO").upper()
    return _LEVELS.get(value, INFO)


def _log(level: str, message: str, *, stream: TextIO | None = None) -> None:
    if _LEVELS[level] < _configured_level():
        return
    if stream is None:
        stream = sys.stdout
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp}  {level:<7} {one_line(message)}", file=stream, flush=True)


def debug(message: str) -> None:
    _log("DEBUG", message)


def info(message: str) -> None:
    _log("INFO", message)


def warning(message: str) -> None:
    _log("WARNING", message)


def error(message: str) -> None:
    _log("ERROR", message)


def write(message: str, *, end: str = "") -> None:
    """Write harness output unchanged, while avoiding direct print calls."""
    sys.stdout.write(message + end)
    sys.stdout.flush()
