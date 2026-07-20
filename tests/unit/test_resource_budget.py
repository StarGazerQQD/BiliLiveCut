"""Resource budget tests — validates acquire_resources and release_resources."""

from __future__ import annotations


def test_acquire_resources_with_real_args() -> None:
    """acquire_resources with valid CPU and memory arguments."""
    from app.core.resource_budget import acquire_resources

    assert isinstance(acquire_resources(cpu=1, memory_mb=256), bool)
    assert isinstance(acquire_resources(cpu=0, memory_mb=0), bool)
    assert isinstance(acquire_resources(cpu=4, memory_mb=8192), bool)


def test_release_resources_with_args() -> None:
    """release_resources handles valid arguments gracefully."""
    from app.core.resource_budget import release_resources

    release_resources(cpu=1, memory_mb=256)
    release_resources(cpu=0, memory_mb=0)
