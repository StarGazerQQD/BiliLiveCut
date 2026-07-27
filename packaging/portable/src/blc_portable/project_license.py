"""Project license identity shared by Portable release builders."""

from __future__ import annotations

import hashlib
from pathlib import Path

PROJECT_LICENSE_ID = "MIT"
PROJECT_COPYRIGHT = "Copyright (c) 2026 StarGazerQQD"
PROJECT_ROOT = Path(__file__).resolve().parents[4]
PROJECT_LICENSE_PATH = PROJECT_ROOT / "LICENSE"

_REQUIRED_MARKERS = (
    "MIT License",
    PROJECT_COPYRIGHT,
    "Permission is hereby granted, free of charge",
    'THE SOFTWARE IS PROVIDED "AS IS"',
)


def load_project_license(path: Path = PROJECT_LICENSE_PATH) -> bytes:
    """Load and validate the canonical project license.

    :param path: License file to load.
    :returns: Exact UTF-8 bytes to embed in release artifacts.
    :raises RuntimeError: The file is missing, invalid UTF-8, or not the expected MIT grant.
    """
    try:
        content = path.read_bytes()
        text = content.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Project license is missing or unreadable: {path}") from exc

    missing = [marker for marker in _REQUIRED_MARKERS if marker not in text]
    if missing:
        raise RuntimeError(f"Project license is invalid: missing {missing!r} in {path}")
    return content


def project_license_sha256(path: Path = PROJECT_LICENSE_PATH) -> str:
    """Return the SHA-256 of the validated canonical project license."""
    return hashlib.sha256(load_project_license(path)).hexdigest()
