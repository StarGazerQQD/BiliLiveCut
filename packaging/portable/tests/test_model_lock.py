"""Model lock identity regression tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PORTABLE_DIR = Path(__file__).resolve().parent.parent
if str(_PORTABLE_DIR / "src") not in sys.path:
    sys.path.insert(0, str(_PORTABLE_DIR / "src"))

from blc_portable.model_lock import compute_model_lock_sha256  # noqa: E402


def test_model_lock_sha256_is_stable_across_line_endings(tmp_path: Path) -> None:
    """LF and CRLF checkouts of the same JSON must have one identity."""
    lf_path = tmp_path / "lf.json"
    crlf_path = tmp_path / "crlf.json"
    lf_path.write_bytes(b'{\n  "schema_version": 4\n}\n')
    crlf_path.write_bytes(b'{\r\n  "schema_version": 4\r\n}\r\n')

    assert compute_model_lock_sha256(lf_path) == compute_model_lock_sha256(crlf_path)


def test_model_lock_sha256_detects_content_changes(tmp_path: Path) -> None:
    """A semantic lock-file byte change must still alter the identity."""
    original = tmp_path / "original.json"
    changed = tmp_path / "changed.json"
    original.write_bytes(b'{"schema_version": 4}\n')
    changed.write_bytes(b'{"schema_version": 5}\n')

    assert compute_model_lock_sha256(original) != compute_model_lock_sha256(changed)


def test_model_lock_sha256_rejects_missing_file(tmp_path: Path) -> None:
    """Missing model locks must fail closed."""
    with pytest.raises(FileNotFoundError):
        compute_model_lock_sha256(tmp_path / "missing.json")
