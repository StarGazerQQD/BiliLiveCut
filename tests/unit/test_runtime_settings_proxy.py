from __future__ import annotations

from contextlib import nullcontext
from typing import Literal

import pytest

from app.core import config
from app.core.config import settings
from app.core.runtime_settings import settings_scope


@pytest.mark.parametrize("target", ["direct", "module", "dotted"])
@pytest.mark.parametrize("fail", [False, True])
def test_proxy_patch_restores_environment_baseline(
    temp_db: None, target: Literal["direct", "module", "dotted"], fail: bool
) -> None:
    base = config.get_settings()
    original = base.worker_shutdown_timeout_seconds
    updated = original + 1
    outcome = pytest.raises(RuntimeError, match="operation failed") if fail else nullcontext()

    with outcome, pytest.MonkeyPatch.context() as patch:
        if target == "direct":
            patch.setattr(settings, "worker_shutdown_timeout_seconds", updated)
        elif target == "module":
            patch.setattr(config.settings, "worker_shutdown_timeout_seconds", updated)
        else:
            patch.setattr("app.core.config.settings.worker_shutdown_timeout_seconds", updated)
        assert config.get_settings() is base
        assert base.worker_shutdown_timeout_seconds == updated
        assert settings.worker_shutdown_timeout_seconds == updated
        if fail:
            raise RuntimeError("operation failed")

    assert base.worker_shutdown_timeout_seconds == original
    assert settings.worker_shutdown_timeout_seconds == original


def test_nested_proxy_patches_restore_each_environment_value(temp_db: None) -> None:
    base = config.get_settings()
    original = base.worker_shutdown_timeout_seconds

    with pytest.MonkeyPatch.context() as outer:
        outer.setattr(settings, "worker_shutdown_timeout_seconds", original + 1)
        with pytest.MonkeyPatch.context() as inner:
            inner.setattr("app.core.config.settings.worker_shutdown_timeout_seconds", original + 2)
            assert settings.worker_shutdown_timeout_seconds == original + 2
            assert base.worker_shutdown_timeout_seconds == original + 2
        assert settings.worker_shutdown_timeout_seconds == original + 1
        assert base.worker_shutdown_timeout_seconds == original + 1

        with pytest.raises(RuntimeError, match="nested operation failed"), pytest.MonkeyPatch.context() as inner:
            inner.setattr(config.settings, "worker_shutdown_timeout_seconds", original + 3)
            assert settings.worker_shutdown_timeout_seconds == original + 3
            assert base.worker_shutdown_timeout_seconds == original + 3
            raise RuntimeError("nested operation failed")
        assert settings.worker_shutdown_timeout_seconds == original + 1
        assert base.worker_shutdown_timeout_seconds == original + 1

    assert settings.worker_shutdown_timeout_seconds == original
    assert base.worker_shutdown_timeout_seconds == original


def test_proxy_does_not_expose_environment_or_task_dictionary(temp_db: None) -> None:
    with pytest.raises(TypeError, match="__dict__"):
        vars(settings)
    with settings_scope(fresh=True):
        with pytest.raises(TypeError, match="__dict__"):
            vars(settings)
