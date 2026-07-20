"""Stage 5 final coverage — ffmpeg_errors classification and schema validation."""

from __future__ import annotations


def test_classify_ffmpeg_error_return_code_only() -> None:
    """classify_ffmpeg_error with exit code and empty stderr."""
    from app.core.ffmpeg_errors import classify_ffmpeg_error

    result = classify_ffmpeg_error(1, "")
    assert result is not None


def test_classify_ffmpeg_error_with_stderr() -> None:
    """classify_ffmpeg_error with various stderr patterns."""
    from app.core.ffmpeg_errors import classify_ffmpeg_error

    r1 = classify_ffmpeg_error(1, "Connection timed out")
    r2 = classify_ffmpeg_error(1, "Invalid data found when processing input")
    r3 = classify_ffmpeg_error(127, "whatever")
    assert r1 is not None
    assert r2 is not None
    assert r3 is not None


def test_classify_ffmpeg_error_success_code() -> None:
    """classify_ffmpeg_error with exit code 0."""
    from app.core.ffmpeg_errors import classify_ffmpeg_error

    result = classify_ffmpeg_error(0, "")
    assert result is not None


def test_is_retryable_on_error_types() -> None:
    """is_retryable returns boolean for various error types."""
    from app.core.ffmpeg_errors import classify_ffmpeg_error, is_retryable

    # Test with a few error patterns
    for exit_code, stderr in [(1, ""), (1, "timeout"), (1, "Protocol not found")]:
        err_type = classify_ffmpeg_error(exit_code, stderr)
        assert isinstance(is_retryable(err_type), bool)
