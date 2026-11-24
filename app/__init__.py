"""BiliLiveCut:AI 直播实时切片系统。

针对 Bilibili 直播的全自动工作流:实时录制、转写、识别高光、生成切片与文案。
本包为后端主工程,按模块拆分(sources / recording / analysis / clipping /
publishing / pipeline / web),通过数据库与任务队列解耦。
"""

from app._build_identity import RELEASE_VERSION as __version__
from app._build_identity import VERSION_LABEL as __version_label__


def version_label(version: str | None = None) -> str:
    """Return the build label, or format an explicitly supplied version."""
    if version is None or version == __version__:
        return __version_label__
    base, _, suffix = version.partition("-")
    return f"V{base} {suffix.title()}".rstrip()
