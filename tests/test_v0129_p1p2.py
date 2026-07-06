"""v0.1.12.9-alpha P1/P2 行为测试。

覆盖:
- FFmpeg 错误分类
- Bilibili 风控熔断
- 弹幕分级采样
- 磁盘保护
- 脱敏
- 配置校验
"""

from __future__ import annotations

# ══════════════════════════════════════════════════════════════════════
# FFmpeg 错误分类测试
# ══════════════════════════════════════════════════════════════════════

class TestFfmpegErrorClassification:
    """FFmpeg 错误分类器测试。"""

    def test_missing_binary(self) -> None:
        """缺失 FFmpeg 二进制文件应被正确分类。"""
        from app.core.ffmpeg_errors import FfmpegErrorType, classify_ffmpeg_error

        assert classify_ffmpeg_error(255, "ffmpeg: No such file or directory") == FfmpegErrorType.MISSING_BINARY
        assert classify_ffmpeg_error(255, "ffmpeg: command not found") == FfmpegErrorType.MISSING_BINARY

    def test_disk_full(self) -> None:
        """磁盘满错误应被正确分类。"""
        from app.core.ffmpeg_errors import FfmpegErrorType, classify_ffmpeg_error

        assert classify_ffmpeg_error(1, "No space left on device") == FfmpegErrorType.DISK_FULL
        assert classify_ffmpeg_error(1, "Error writing file: Disk full") == FfmpegErrorType.DISK_FULL

    def test_permission_denied(self) -> None:
        """权限拒绝错误应被正确分类。"""
        from app.core.ffmpeg_errors import FfmpegErrorType, classify_ffmpeg_error

        assert classify_ffmpeg_error(1, "Permission denied") == FfmpegErrorType.PERMISSION_DENIED

    def test_invalid_argument(self) -> None:
        """无效参数错误应被正确分类。"""
        from app.core.ffmpeg_errors import FfmpegErrorType, classify_ffmpeg_error

        assert classify_ffmpeg_error(1, "Invalid argument") == FfmpegErrorType.INVALID_ARGUMENT
        assert classify_ffmpeg_error(1, "Unrecognized option '--bad'") == FfmpegErrorType.INVALID_ARGUMENT

    def test_transient_network(self) -> None:
        """临时网络错误应被正确分类。"""
        from app.core.ffmpeg_errors import FfmpegErrorType, classify_ffmpeg_error

        assert classify_ffmpeg_error(1, "Connection refused") == FfmpegErrorType.TRANSIENT_NETWORK
        assert classify_ffmpeg_error(1, "Network is unreachable") == FfmpegErrorType.TRANSIENT_NETWORK
        assert classify_ffmpeg_error(1, "Name or service not known") == FfmpegErrorType.TRANSIENT_NETWORK
        assert classify_ffmpeg_error(1, "Connection timed out") == FfmpegErrorType.TRANSIENT_NETWORK

    def test_upstream_unavailable(self) -> None:
        """上游不可用错误应被正确分类。"""
        from app.core.ffmpeg_errors import FfmpegErrorType, classify_ffmpeg_error

        assert classify_ffmpeg_error(1, "Server returned 404 Not Found") == FfmpegErrorType.UPSTREAM_UNAVAILABLE
        assert classify_ffmpeg_error(1, "HTTP error 500") == FfmpegErrorType.UPSTREAM_UNAVAILABLE

    def test_corrupted_input(self) -> None:
        """损坏输入应被正确分类。"""
        from app.core.ffmpeg_errors import FfmpegErrorType, classify_ffmpeg_error

        assert classify_ffmpeg_error(1, "Invalid data found when processing input") == FfmpegErrorType.CORRUPTED_INPUT
        assert classify_ffmpeg_error(1, "moov atom not found") == FfmpegErrorType.CORRUPTED_INPUT

    def test_unsupported_codec(self) -> None:
        """不支持的编码器应被正确分类。"""
        from app.core.ffmpeg_errors import FfmpegErrorType, classify_ffmpeg_error

        assert classify_ffmpeg_error(1, "Unknown encoder 'libx265_notfound'") == FfmpegErrorType.UNSUPPORTED_CODEC
        assert classify_ffmpeg_error(1, "Decoder not found") == FfmpegErrorType.UNSUPPORTED_CODEC

    def test_unknown(self) -> None:
        """未知错误应归类为 UNKNOWN。"""
        from app.core.ffmpeg_errors import FfmpegErrorType, classify_ffmpeg_error

        assert classify_ffmpeg_error(1, "Some random error message") == FfmpegErrorType.UNKNOWN

    def test_is_retryable(self) -> None:
        """可重试/不可重试错误分类正确。"""
        from app.core.ffmpeg_errors import FfmpegErrorType, is_retryable

        assert is_retryable(FfmpegErrorType.TRANSIENT_NETWORK) is True
        assert is_retryable(FfmpegErrorType.UPSTREAM_UNAVAILABLE) is True
        assert is_retryable(FfmpegErrorType.DISK_FULL) is False
        assert is_retryable(FfmpegErrorType.MISSING_BINARY) is False
        assert is_retryable(FfmpegErrorType.PERMISSION_DENIED) is False
        assert is_retryable(FfmpegErrorType.INVALID_ARGUMENT) is False
        assert is_retryable(FfmpegErrorType.CORRUPTED_INPUT) is False
        assert is_retryable(FfmpegErrorType.UNSUPPORTED_CODEC) is False
        assert is_retryable(FfmpegErrorType.UNKNOWN) is False
        assert is_retryable(FfmpegErrorType.CANCELLED) is False


# ══════════════════════════════════════════════════════════════════════
# 弹幕分级采样测试
# ══════════════════════════════════════════════════════════════════════

class TestDanmakuSampling:
    """弹幕分级采样器测试。"""

    def test_super_chat_always_kept(self) -> None:
        """SC 应始终保留。"""
        from app.analysis.danmaku_sampling import DanmakuSampler
        from app.db.models import DanmakuType

        sampler = DanmakuSampler()
        kept = sum(1 for _ in range(100) if sampler.should_keep(DanmakuType.SUPERCHAT))
        assert kept == 100, f"SC 应 100% 保留, 实际保留 {kept}/100"

    def test_interact_always_kept(self) -> None:
        """互动应始终保留。"""
        from app.analysis.danmaku_sampling import DanmakuSampler
        from app.db.models import DanmakuType

        sampler = DanmakuSampler()
        kept = sum(1 for _ in range(100) if sampler.should_keep(DanmakuType.INTERACT))
        assert kept == 100, f"舰长应 100% 保留, 实际保留 {kept}/100"

    def test_normal_danmaku_sampled(self) -> None:
        """普通弹幕应按比例采样。"""
        from app.analysis.danmaku_sampling import DanmakuSampler
        from app.db.models import DanmakuType

        sampler = DanmakuSampler()
        kept = sum(1 for _ in range(1000) if sampler.should_keep(DanmakuType.DANMAKU))
        # 期望 30%, 允许 ±10% 容差
        assert 200 <= kept <= 400, f"普通弹幕期望保留 ~30%, 实际保留 {kept}/1000"

    def test_high_density_reduces_normal_rate(self) -> None:
        """高密度下普通弹幕保留率降低。"""
        from app.analysis.danmaku_sampling import DanmakuSampler
        from app.db.models import DanmakuType

        sampler = DanmakuSampler()
        # 模拟高密度: 记录 2000 条弹幕 (远大于 1000/min)
        for _ in range(2000):
            sampler.record()
        kept = sum(1 for _ in range(500) if sampler.should_keep(DanmakuType.DANMAKU))
        # 高密度下期望 ~10%
        assert 20 <= kept <= 100, f"高密度下普通弹幕期望保留 ~10%, 实际保留 {kept}/500"

    def test_per_room_isolation(self) -> None:
        """每个房间有独立的采样器。"""
        from app.analysis.danmaku_sampling import get_sampler

        s1 = get_sampler(123)
        s2 = get_sampler(456)
        assert s1 is not s2, "不同房间应有独立采样器"
        assert get_sampler(123) is s1, "同一房间应复用同一采样器"


# ══════════════════════════════════════════════════════════════════════
# 敏感信息脱敏测试
# ══════════════════════════════════════════════════════════════════════

class TestSanitize:
    """敏感信息脱敏器测试。"""

    def test_cookie_sanitized(self) -> None:
        """Cookie 应被脱敏。"""
        from app.core.sanitize import sanitize_text

        raw = "SESSDATA=abc123def456ghi789; bili_jct=xyz789abc123"
        result = sanitize_text(raw)
        assert "abc123def456ghi789" not in result, "SESSDATA 值应被脱敏"
        assert "xyz789abc123" not in result, "bili_jct 值应被脱敏"
        assert "SESSDATA=***" in result, "应留下键名"
        assert "bili_jct=***" in result, "应留下键名"

    def test_authorization_sanitized(self) -> None:
        """Authorization 头应被脱敏。"""
        from app.core.sanitize import sanitize_text

        raw = "Authorization: Bearer sk-abc123def456ghi789"
        result = sanitize_text(raw)
        assert "sk-abc123def456ghi789" not in result, "Bearer Token 应被脱敏"

    def test_password_sanitized(self) -> None:
        """密码应被脱敏。"""
        from app.core.sanitize import sanitize_text

        raw = 'password="mySecretP@ssw0rd123"'
        result = sanitize_text(raw)
        assert "mySecretP@ssw0rd123" not in result, "密码应被脱敏"
        assert "***" in result, "应包含脱敏标记"

    def test_url_token_sanitized(self) -> None:
        """URL 中的 token 参数应被脱敏。"""
        from app.core.sanitize import sanitize_text

        raw = "https://example.com/callback?token=abc123def456&other=val"
        result = sanitize_text(raw)
        assert "abc123def456" not in result, "URL token 值应被脱敏"

    def test_api_key_sanitized(self) -> None:
        """API Key 应被脱敏。"""
        from app.core.sanitize import sanitize_text

        raw = 'api_key=sk-1234567890abcdefghij'
        result = sanitize_text(raw)
        assert "sk-1234567890abcdefghij" not in result, "API Key 应被脱敏"

    def test_short_value_fully_masked(self) -> None:
        """短值 (≤6 字符) 应全掩码。"""
        from app.core.sanitize import sanitize_text

        result = sanitize_text('secret="abc"')
        assert "***" in result, "应包含脱敏标记"
        assert "abc" not in result, "值应被脱敏"

    def test_none_returns_none(self) -> None:
        """None 输入应返回 None。"""
        from app.core.sanitize import sanitize_text

        assert sanitize_text(None) is None

    def test_empty_returns_empty(self) -> None:
        """空字符串输入应返回空字符串。"""
        from app.core.sanitize import sanitize_text

        assert sanitize_text("") == ""


# ══════════════════════════════════════════════════════════════════════
# 磁盘保护测试
# ══════════════════════════════════════════════════════════════════════

class TestDiskProtection:
    """两级磁盘保护测试。"""

    def test_check_disk_safe_returns_stats(self) -> None:
        """磁盘检测应返回合理的结果。"""
        from app.pipeline.storage_lifecycle import check_disk_safe

        safe, msg = check_disk_safe()
        assert isinstance(safe, bool), "应返回 bool"
        assert isinstance(msg, str), "应返回描述信息"

    def test_get_disk_usage(self) -> None:
        """磁盘使用情况应包含必要字段。"""
        from app.pipeline.storage_lifecycle import get_disk_usage

        usage = get_disk_usage()
        assert "total_gb" in usage
        assert "free_gb" in usage
        assert "used_gb" in usage
        assert usage["free_gb"] > 0


# ══════════════════════════════════════════════════════════════════════
# 弹幕采样器与 DanmakuType 集成测试
# ══════════════════════════════════════════════════════════════════════

class TestDanmakuSamplingIntegration:
    """弹幕采样集成测试。"""

    def test_gift_high_retention(self) -> None:
        """礼物应有高保留率。"""
        from app.analysis.danmaku_sampling import DanmakuSampler
        from app.db.models import DanmakuType

        sampler = DanmakuSampler()
        kept = sum(1 for _ in range(200) if sampler.should_keep(DanmakuType.GIFT))
        assert kept >= 120, f"礼物保留率应 ≥60%, 实际保留 {kept}/200"

    def test_other_medium_retention(self) -> None:
        """其他类型应有中等保留率。"""
        from app.analysis.danmaku_sampling import DanmakuSampler
        from app.db.models import DanmakuType

        sampler = DanmakuSampler()
        kept = sum(1 for _ in range(200) if sampler.should_keep(DanmakuType.OTHER))
        assert 60 <= kept <= 140, f"OTHER 保留率应 ~50%, 实际保留 {kept}/200"


# ══════════════════════════════════════════════════════════════════════
# 脱敏 Settings repr 测试
# ══════════════════════════════════════════════════════════════════════

class TestSettingsReprSanitize:
    """Settings repr 脱敏测试。"""

    def test_repr_does_not_leak_password(self, monkeypatch) -> None:
        """Settings repr 不应泄露密码。"""
        monkeypatch.setenv("ADMIN_PASSWORD", "secret123")
        from app.core.config import Settings, get_settings

        get_settings.cache_clear()
        s = Settings()
        rep = repr(s)
        assert "secret123" not in rep, f"repr 泄露了密码: {rep}"

    def test_repr_does_not_leak_cookie(self, monkeypatch) -> None:
        """Settings repr 不应泄露 Cookie。"""
        monkeypatch.setenv("BILIBILI_COOKIE", "SESSDATA=abc123def456")
        from app.core.config import Settings, get_settings

        get_settings.cache_clear()
        s = Settings()
        rep = repr(s)
        assert "abc123def456" not in rep, f"repr 泄露了 Cookie: {rep}"
