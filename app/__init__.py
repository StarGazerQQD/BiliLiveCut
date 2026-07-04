"""BiliLiveCut:AI 直播实时切片系统。

针对 Bilibili 直播的全自动工作流:实时录制、转写、识别高光、生成切片与文案。
本包为后端主工程,按模块拆分(sources / recording / analysis / clipping /
publishing / pipeline / web),通过数据库与任务队列解耦。
"""

__version__ = "0.1.8.1b-alpha"
__version_label__ = "V0.1.8.1b Alpha"
