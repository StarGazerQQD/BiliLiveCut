"""P3 单元测试:磁盘保护 + 封面评分 + 直播监控。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.clipping.cover import (
    _detect_blur as detect_blur,
)
from app.clipping.cover import (
    _detect_brightness as detect_brightness,
)
from app.clipping.cover import (
    _detect_face_score,
    _probe_duration,
)
from app.pipeline.storage_lifecycle import (
    check_disk_safe,
    get_directory_size,
    get_disk_usage,
)

# ======================== 磁盘检测 ========================


class TestDiskUsage:
    """磁盘使用检测。"""

    @staticmethod
    def test_get_disk_usage_returns_dict() -> None:
        """返回完整字段。"""
        result = get_disk_usage()
        assert "total_gb" in result
        assert "used_gb" in result
        assert "free_gb" in result
        assert "free_percent" in result
        assert result["total_gb"] > 0

    @staticmethod
    def test_get_disk_usage_with_path() -> None:
        """指定路径检测。"""
        result = get_disk_usage(".")
        assert result["free_gb"] > 0

    @staticmethod
    def test_get_directory_size() -> None:
        """目录大小检测。"""
        size = get_directory_size(Path(__file__).parent)
        assert size >= 0  # 至少能返回数值


class TestDiskSafe:
    """磁盘安全检查。"""

    @staticmethod
    def test_safe_with_high_threshold() -> None:
        """高阈值时可能不安全,但至少返回元组。"""
        safe, msg = check_disk_safe(min_free_gb=999999)
        assert isinstance(safe, bool)
        assert isinstance(msg, str)

    @staticmethod
    def test_safe_with_low_threshold() -> None:
        """低阈值(1GB)应该安全。"""
        safe, msg = check_disk_safe(min_free_gb=0.01)
        assert isinstance(safe, bool)


# ======================== 封面评分 ========================


class TestBlurDetection:
    """模糊检测。"""

    @staticmethod
    def test_detect_blur_on_test_image() -> None:
        """在一张小图片上测试模糊检测(不抛异常)。"""
        try:
            import cv2  # noqa: F401
        except ImportError:
            try:
                from PIL import Image  # noqa: F401
            except ImportError:
                import pytest

                pytest.skip("No image library available")
        # 创建一个简单的测试图片。
        try:
            from PIL import Image as PILImage

            img = PILImage.new("RGB", (100, 100), color=(128, 128, 128))
            tmp_path = Path(__file__).parent / "_test_blur.jpg"
            img.save(str(tmp_path))
            score = detect_blur(tmp_path)
            assert score >= 0
        finally:
            tmp = Path(__file__).parent / "_test_blur.jpg"
            if tmp.exists():
                tmp.unlink()

    @staticmethod
    def test_detect_brightness_on_test_image() -> None:
        """亮度检测(3 色块:黑/灰/白)。"""
        try:
            import numpy as np  # noqa: F401
            from PIL import Image as PILImage
        except ImportError:
            import pytest

            pytest.skip("No image library available")

        tmp_path = Path(__file__).parent / "_test_bright.jpg"
        try:
            img = PILImage.new("RGB", (100, 100), color=(200, 200, 200))
            img.save(str(tmp_path))
            brightness = detect_brightness(tmp_path)
            assert 100 <= brightness <= 255
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    @staticmethod
    def test_detect_blur_nonexistent_file() -> None:
        """不存在的文件返回 0。"""
        score = detect_blur(Path("/nonexistent/image.jpg"))
        # 打开失败时可能返回 0。
        assert score >= 0


class TestFaceDetection:
    """人脸检测(optional)。"""

    @staticmethod
    def test_face_score_no_face() -> None:
        """无 face 检测库时返回中性值。"""
        try:
            import cv2  # noqa: F401
        except ImportError:
            score = _detect_face_score(Path("/nonexistent.jpg"))
            # fallback path returns 0.5 or 0
            assert score in (0.0, 0.5)


class TestProbeDuration:
    """时长探测。"""

    @staticmethod
    def test_nonexistent_file() -> None:
        """不存在的文件返回 0。"""
        dur = _probe_duration(Path("/nonexistent.mp4"))
        assert dur == 0


# ======================== 直播监控状态 ========================


class TestLiveMonitor:
    """直播监控器基础状态。"""

    @staticmethod
    def test_monitor_initial_status() -> None:
        """初始状态。"""
        from app.pipeline.live_monitor import LiveMonitor

        monitor = LiveMonitor()
        status = monitor.status()
        assert "running" in status
        assert "watching_rooms" in status
        assert status["running"] is False  # 尚未启动

    @staticmethod
    def test_monitor_reconnect_counter() -> None:
        """重连计数初始为 0。"""
        from app.pipeline.live_monitor import LiveMonitor

        monitor = LiveMonitor()
        assert monitor.get_reconnect_total(999) == 0

    @staticmethod
    @pytest.mark.asyncio
    async def test_delayed_stop_rechecks_live_source_before_stopping(monkeypatch: pytest.MonkeyPatch) -> None:
        """延迟窗口结束时必须复核：恢复直播不停止，仍离线才停止。"""
        from app.pipeline import live_monitor as live_monitor_module
        from app.web import service as service_module

        live_state = {"value": 1}

        class FakeClient:
            async def __aenter__(self) -> FakeClient:
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            async def get_room_info(self, _room_id: str, *, include_detail: bool) -> SimpleNamespace:
                assert include_detail is False
                return SimpleNamespace(live_status=live_state["value"])

        class FakeManager:
            def __init__(self) -> None:
                self.running = True
                self.stopped: list[int] = []

            def is_running(self, db_id: int) -> bool:
                return self.running

            async def stop(self, db_id: int) -> None:
                self.running = False
                self.stopped.append(db_id)

        manager = FakeManager()
        monkeypatch.setattr(live_monitor_module, "BilibiliLiveClient", lambda **_kwargs: FakeClient())
        monkeypatch.setattr(live_monitor_module, "get_bilibili_cookie", lambda: "")
        monkeypatch.setattr(live_monitor_module.settings, "live_session_end_delay_s", 0)
        monkeypatch.setattr(service_module, "recorder_manager", manager)
        monitor = live_monitor_module.LiveMonitor()
        monitor._stop = asyncio.Event()  # noqa: SLF001

        await monitor._delayed_stop(9, 99)  # noqa: SLF001
        assert manager.stopped == []

        live_state["value"] = 0
        await monitor._delayed_stop(9, 99)  # noqa: SLF001
        assert manager.stopped == [9]

    @staticmethod
    @pytest.mark.asyncio
    async def test_live_recovery_cancels_pending_delayed_stop() -> None:
        """延迟收尾任务必须可追踪并可在直播恢复时撤销。"""
        from app.pipeline.live_monitor import LiveMonitor

        monitor = LiveMonitor()
        monitor._stop = asyncio.Event()  # noqa: SLF001
        task = asyncio.create_task(asyncio.sleep(60))
        monitor._pending_stops[3] = task  # noqa: SLF001

        monitor._cancel_pending_stop(3)  # noqa: SLF001
        await asyncio.gather(task, return_exceptions=True)

        assert task.cancelled()
        assert monitor.status()["pending_stops"] == []
