"""Portable-safe atomic filesystem operations."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

WINDOWS_TRANSIENT_REPLACE_ERRORS = frozenset({5, 32, 33})
DEFAULT_REPLACE_ATTEMPTS = 7
DEFAULT_REPLACE_DELAY_SECONDS = 0.05
MAX_REPLACE_DELAY_SECONDS = 0.5


def replace_with_retry(
    source: str | Path,
    target: str | Path,
    *,
    attempts: int = DEFAULT_REPLACE_ATTEMPTS,
    initial_delay: float = DEFAULT_REPLACE_DELAY_SECONDS,
) -> None:
    """Atomically replace a path, retrying only transient Windows lock errors.

    Antivirus and indexing processes can briefly hold a newly written file or
    directory on Windows. Other platforms and non-transient errors are raised
    immediately so permanent permission and path failures remain visible.

    :param source: Source file or directory.
    :param target: Destination file or directory.
    :param attempts: Total replace attempts, including the first call.
    :param initial_delay: Initial retry delay in seconds.
    :raises ValueError: If retry arguments are invalid.
    :raises OSError: If replacement fails permanently or retries are exhausted.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    if initial_delay < 0:
        raise ValueError("initial_delay must not be negative")

    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            transient = sys.platform == "win32" and exc.winerror in WINDOWS_TRANSIENT_REPLACE_ERRORS
            if not transient or attempt == attempts - 1:
                raise
            delay = min(initial_delay * (2**attempt), MAX_REPLACE_DELAY_SECONDS)
            time.sleep(delay)
