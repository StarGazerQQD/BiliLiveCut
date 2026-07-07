"""BiliLiveCut:AI 直播实时切片系统。

针对 Bilibili 直播的全自动工作流:实时录制、转写、识别高光、生成切片与文案。
本包为后端主工程,按模块拆分(sources / recording / analysis / clipping /
publishing / pipeline / web),通过数据库与任务队列解耦。
"""

__version__ = "0.1.14-alpha"
__version_label__ = "V0.1.14 Alpha"


def version_label(version: str | None = None) -> str:
    """动态生成版本展示标签 (V0.1.12.7 — 统一版本真源)。

    :param version: 版本字符串, 默认使用 __version__。
    :returns: 格式化的版本标签, 如 "V0.1.12.9 Alpha"。
    """
    ver = version or __version__
    # 去掉 "-alpha"/"-beta" 等后缀, 转大写前缀
    base = ver.split("-")[0] if "-" in ver else ver
    return f"V{base} Alpha"
