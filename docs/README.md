# 文档索引

项目简介和安装入口见[项目 README](../README.md)。这里按使用和维护需求组织持续更新的文档。

## 使用与部署

| 文档 | 内容 |
| --- | --- |
| [Portable 小白使用说明](../packaging/portable/USER_GUIDE_ZH.md) | 下载、校验、首次启动、模型准备和第一次录制 |
| [使用指南](usage.md) | 源码安装、CLI、录制分析、Web、审核、上传和排错 |
| [录播导入](recording-import.md) | 已有视频和 XML、JSON、SRT、ASS 弹幕的预处理、本地分析与恢复 |
| [配置参考](configuration.md) | 配置字段、作用范围、持久化和生效时间 |
| [Docker 部署](../packaging/docker/README.md) | 容器构建、运行与数据目录 |

## 开发与维护

| 文档 | 内容 |
| --- | --- |
| [开发与维护](development.md) | 验证命令、源码冻结、发行维护和文档约定 |
| [热点检测器](hotspot-detector.md) | 事件生命周期、信号融合、成片评分与 ASR 降级 |
| [原生加速模块](native-acceleration.md) | C、Cython、Rust 边界、构建与诊断 |
| [Portable 构建说明](../packaging/portable/README.md) | Lite/Full、Engine Pack、Payload、Runtime 与发行检查 |
| [插件开发](../plugin/README.md) | 插件 API、清单 Schema 和示例 |

## 版本历史

当前版本系列的更新集中在 [CHANGELOG](../CHANGELOG.md)，历史系列见[归档索引](changelog/CHANGELOG_INDEX.md)。历史记录描述当时行为，当前使用方式以以上指南为准。
