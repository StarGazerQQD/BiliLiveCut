# Changelog

## V0.1.1 Alpha (2026-07-02)

### 变更

- 新增 `launcher.exe` 即插即用启动器,无需 `.ps1`/`.bat` 脚本即可运行
- 显式将 `.env.example` 排除出 `.gitignore`,确保配置模板正常入库
- `.gitattributes` 规范化行尾(LF 入库 / 自动 CRLF Windows 检出)
- 修复 `Recorder.run()` 中 `backoff` 变量未初始化导致 `NameError` 的问题

## V0.1.0 Alpha (2026-07-01)

首个可运行 Alpha 版本,涵盖 B 站 AI 直播实时切片全链路 MVP。

### 功能

- 直播源获取、FFmpeg 录制与分片、断流重连
- Whisper 本地转写、弹幕采集、网感资料库与定时采集
- 多维度高光评分、自动切片与后处理、LLM 文案生成
- OpenAI 兼容多模型 LLM 与失败回退
- Web 管理控制台(FastAPI)
- 上传队列与 Docker 部署
- 即插即用 `Public/` 分发包(Whisper 模型、ffmpeg、离线 wheel、一键 setup/check)

### 说明

- 版本号:PEP 440 `0.1.0-alpha`,展示名 **V0.1.0 Alpha**
- Alpha 阶段 API 与配置可能变动,生产使用前请自行评估
