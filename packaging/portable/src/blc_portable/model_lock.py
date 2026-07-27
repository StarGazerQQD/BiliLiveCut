"""Cross-platform identity helpers for the Portable model lock."""

from __future__ import annotations

import hashlib
from pathlib import Path

PORTABLE_DIR = Path(__file__).resolve().parents[2]
MODEL_LOCK_PATH = PORTABLE_DIR / "config" / "model_sources.lock.json"


def compute_model_lock_sha256(path: Path = MODEL_LOCK_PATH) -> str:
    """Return a stable SHA-256 after normalizing text line endings to LF.

    Git checkouts may expose the same tracked JSON as LF or CRLF. The model
    lock identity must describe the tracked content rather than the checkout
    platform, so only line-ending bytes are normalized before hashing.

    :param path: Model source lock file.
    :return: Lowercase hexadecimal SHA-256 digest.
    """
    content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(content).hexdigest()
