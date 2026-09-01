# Changelog

## 未发布

- 暂无。

## V0.1.18.1 Alpha (2026-08-31)

### 破坏性收口

- **compatibility/current-only**: Alpha 运行时移除 0.1.17.x 数据库迁移、schema-5 Engine Pack 已安装清单迁移、跨发行版 Engine Pack 安装入口和无 `HotspotEvent` 候选的时间线兼容节点；数据库、Engine Pack、安装清单和时间线现在只接受当前结构，历史数据不会被推断、备份或改写。
- **native/build-contract**: PyO3 构建只绑定当前虚拟环境解释器，不再启用 ABI3 向前兼容逃生开关；不受支持的解释器必须直接失败。

### 原生加速

- **native/rust**: 候选聚类相似度矩阵和弹幕文本特征改由当前 `_rust_speedups` 扩展执行；聚类使用 rayon 并行，文本特征以单遍扫描计算复读率、标点强度、高情绪命中率和代表消息。
- **native/cython**: 音频局部峰值筛选、连续静音区间提取和热点滚动历史稳健增幅改由当前 `_cython_speedups` 扩展执行；业务入口按函数选择原生后端，并保留逐项等价的 Python 参考实现用于无编译环境和一致性测试。
- **native/namespace**: C、Cython、Rust 扩展统一收口到 `app.accelerators`；删除旧 `app.analysis` 原生模块路径和函数别名，Portable Payload 只接受当前 Python ABI 的三个原生模块。

### 版本与发布

- **version/release**: Python、C/Cython、Rust、Portable、Engine Pack、Docker、GitHub Actions、测试与用户文档统一升级为 `0.1.18.1-alpha`；GitHub Release 标签固定为 `v0.1.18.1-Alpha`。

## V0.1.18.0 Alpha (2026-08-31)

### Portable Runtime 与模型资产

- **portable/provisioning-interpreter**: 首次在线模型准备统一由已完成依赖安装的 `.venv` Python 子进程执行，并在下载前一次性预检 `huggingface_hub` 与 `modelscope`；失败报告保留解释器、退出码、stdout、stderr 和根异常，不再由 Frozen Launcher 重复报四次缺包。
- **portable/venv-recovery**: 明确区分可复用的 Python 3.11/3.12、真实不支持的 Python，以及程序管理目录内损坏或不完整的 `.venv`；只对可确认归属的损坏环境自动重建，依赖安装中断则在原环境幂等续装。
- **engine-assets/content-identity**: Engine Pack 与应用版本彻底解耦。四个 ASR 引擎按不可变来源身份、必需组件和逐文件内容指纹独立校验、复用和更新；应用版本、ZIP 文件名、构建时间与源码提交不参与模型兼容判断。
- **engine-assets/recovery**: 在线准备改为逐引擎 staging、校验和原子提交；单个引擎中断不会破坏其他已完成模型。经过审计的 `0.1.17.4-alpha` 已安装模型可原地重哈希迁移，内容未变时零网络请求，只有身份变化的引擎会重新准备。
- **release/production-smoke**: Release Gate 新增真实 Launcher 编排测试，覆盖空目录在线分支、子进程解释器边界、准备中断恢复、损坏 venv 自愈、真实 Python 3.14 拒绝、旧模型零下载复用、单引擎更新和离线 Engine Pack。

### Event-first 高光架构

- **database/hotspot-event**: 新增一等 `HotspotEvent`、事件状态与持久化原语；提供幂等的 0.1.17.4 → 0.1.18 schema 迁移，保留房间、场次、原片、转写、候选、审核和成片数据。
- **analysis/hotspot-detector**: 以固定时间桶和滚动基线融合弹幕、音频、SenseVoice、ASR 与趋势信号，先召回 provisional 热点；缺失信号会重归一化而不是按零分惩罚，ASR 完全不可用时仍可形成事件。
- **analysis/asr-priority**: 移除 ASR hard gate。热点窗口优先进入转写队列，后台完整 ASR 继续独立运行；低质量或缺失转写只降低语义证据，不会阻断音频/弹幕事件链。
- **analysis/event-lifecycle**: 热点按真实时间更新、跨连续 RawSegment 合并并确认；录制缺口严格阻止跨越，一个分段允许多个无关事件，代表弹幕按稳定证据规则选取。
- **analysis/event-enricher**: 事件补全使用带证据 ID 的弹幕、ASR、音频与趋势证据束，要求结构化 LLM 输出；标题、摘要和语义置信度均可追溯，证据不足时保守降级而不虚构事实。
- **analysis/clip-scorer**: 成片分独立于热度，按事件完整性、反应、语义、可剪性和证据覆盖计算；只有达到房间阈值的事件才创建 `HighlightCandidate`，边界从完整事件前后文扩展并限制在连续媒体范围内。
- **web/event-timeline**: 场次时间线以活动热点事件为主数据。未达到成片阈值的事件仍展示热度、成片分、语义置信度、证据覆盖与代表弹幕，但没有审核入口；关联候选只显示一次，既有 Review / Render / Publish 链路保持不变。

### 设置与发布

- **web/launch-port**: 设置页可持久化下次启动 Web 端口（1～65535），并同时显示当前端口、下次端口和是否需要重启；Launcher 从根目录 `config/launcher.json` 原子读取，继续只监听 `127.0.0.1`，端口占用时明确失败而不静默换端口。
- **web/localhost-security**: localhost 无密码模式使用标准 authority 解析，接受 `localhost`、`127.0.0.1` 与 `[::1]`，拒绝任意 Host 和跨源修改请求。
- **version/release**: Python、C/Cython、Rust、Portable、Engine Pack、Docker、工作流、测试与用户文档统一升级为 `0.1.18.0-alpha`；GitHub Release 标签固定为 `v0.1.18.0-Alpha`。

---

历史版本归档见 [docs/changelog/CHANGELOG_INDEX.md](docs/changelog/CHANGELOG_INDEX.md)。
