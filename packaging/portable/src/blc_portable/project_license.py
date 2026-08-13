"""Project license identity shared by Portable release builders."""

from __future__ import annotations

import hashlib
import sys
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


def _resolve_project_license_path() -> Path:
    """Resolve the canonical license in source and frozen launcher modes."""
    if getattr(sys, "frozen", False):
        bundle_root = getattr(sys, "_MEIPASS", None)
        if bundle_root:
            return Path(bundle_root) / "LICENSE"
    return PROJECT_LICENSE_PATH


def load_project_license(path: Path | None = None) -> bytes:
    """Load and validate the canonical project license.

    :param path: Explicit license file to load, or ``None`` to resolve the current runtime resource.
    :returns: Exact UTF-8 bytes to embed in release artifacts.
    :raises RuntimeError: The file is missing, invalid UTF-8, or not the expected MIT grant.
    """
    resolved_path = _resolve_project_license_path() if path is None else path
    try:
        content = resolved_path.read_bytes()
        text = content.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Project license is missing or unreadable: {resolved_path}") from exc

    missing = [marker for marker in _REQUIRED_MARKERS if marker not in text]
    if missing:
        raise RuntimeError(f"Project license is invalid: missing {missing!r} in {resolved_path}")
    return content


def project_license_sha256(path: Path | None = None) -> str:
    """Return the SHA-256 of the validated canonical project license."""
    return hashlib.sha256(load_project_license(path)).hexdigest()
