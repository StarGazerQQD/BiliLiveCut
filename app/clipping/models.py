"""切片生成与后处理。

把一个高光候选(可能跨多个原始片段)生成为可投稿的 MP4:

1. 选出覆盖候选时间区间的原始片段并用 FFmpeg concat 拼接;
2. 按候选的精确起止时间(含上下文留白)精剪;
3. 后处理:响度标准化 / 去首尾静默 /(可选)竖屏重构 /(可选)烧录字幕;
4. 抽取封面帧;
5. 探测时长/分辨率,计算内容指纹,写入 ``final_clips`` 并更新候选状态。

所有 FFmpeg 参数均在代码中逐项注释说明。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import settings

# 竖屏目标分辨率(适合手机端短视频)。
_VERT_W, _VERT_H = 1080, 1920


@dataclass(slots=True)
class ClipOptions:
    """切片后处理选项。

    :param loudnorm: 是否做响度标准化。
    :param remove_silence: 是否去除首尾静默。
    :param vertical: 是否竖屏重构。
    :param subtitle: 是否烧录字幕。
    :param max_duration_s: 最大时长(秒)。
    :param crf: x264 质量(0-51)。
    :param preset: x264 编码速度档。
    """

    loudnorm: bool = True
    remove_silence: bool = False
    vertical: bool = False
    subtitle: bool = False
    max_duration_s: int = 180
    crf: int = 20
    preset: str = "veryfast"

    @classmethod
    def from_settings(cls) -> ClipOptions:
        """从全局配置构造默认选项。

        :returns: 依据 ``.env`` 的 :class:`ClipOptions`。
        """
        return cls(
            loudnorm=settings.clip_loudnorm,
            remove_silence=settings.clip_remove_silence,
            vertical=settings.clip_vertical,
            subtitle=settings.clip_subtitle,
            max_duration_s=settings.clip_max_duration_s,
            crf=settings.clip_video_crf,
            preset=settings.clip_preset,
        )
