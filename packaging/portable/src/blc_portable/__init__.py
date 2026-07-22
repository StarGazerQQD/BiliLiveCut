"""BiliLiveCut Portable — 便携版构建与启动工具包。

本包包含 Portable 发行版的核心逻辑，分为以下子包：

- ``launcher``: 启动器主逻辑、环境检测、Payload/Engine Pack 安装
- ``payload``: Payload 清单、源码快照、构建与校验
- ``engine_pack``: 引擎模型包的目录、清单、构建与安装
- ``builders``: Lite/Full 构建入口
- ``util``: 底层工具 (归档、原子操作、Git、哈希、路径)
"""

__version__ = "0.1.15.1-alpha"
