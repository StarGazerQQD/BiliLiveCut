# CHANGELOG — 0.1.15 系列

> 此文件已从主 CHANGELOG.md 归档。原始版本详见 Git 历史。

## V0.1.15.3 Alpha (2026-07-27)

### 修复

- **release**: Lite 首次安装 smoke 驱动在入口将 stdout/stderr 切换为 UTF-8，修复 Windows runner 使用 `cp1252` 回显中文 Launcher 日志时触发 `UnicodeEncodeError`。
- **release**: Lite Doctor 冒烟在验证预期失败摘要后显式返回成功，避免 PowerShell 保留原生命令的预期非零退出码而误判整个 Release step 失败。
- **release**: Release 标签校验与 GitHub prerelease 判定统一使用小写规范化版本，兼容已有的 `-Alpha` 标签并避免被误判为正式版。
- **ci/package**: CI 与 Release 在直接执行 `setup.py build_ext` 前显式安装 `setuptools>=77` 和固定版 Cython，修复 Windows Python 3.11 runner 使用旧构建后端时拒绝 SPDX `license = "MIT"` 的问题。
- **portable/release**: Engine Pack 模型锁摘要统一按 LF 规范化换行后计算，消除同一 JSON 在 Windows CRLF 与 GitHub Actions LF checkout 下产生不同 SHA-256 的跨平台失败。
- **license/release**: 项目代码正式采用 MIT License（Copyright (c) 2026 StarGazerQQD），并将许可证纳入 Python 包、Payload、Portable Lite/Full、GitHub Release 与发布完整性门禁。
- **release**: sdist 明确收录前端 ES Module 交互检查脚本，消除源码包与版本控制文件集合不一致。
- **release**: Full 跨制品校验仅匹配发行根目录直属许可证，避免将 Portable Python 随附的第三方 `LICENSE.txt` 误判为重复项目许可证。
- **release**: Engine Pack 内嵌元数据现在必须与当前模型锁 SHA-256 完全一致，避免版本升级后继续携带旧锁摘要。
- **docs**: 修正 Portable Lite 构建命令示例，并增加禁止文档重新引入过期命令的回归测试。

## V0.1.15.2 Alpha (2026-07-22)

### 修复

- **portable**: Launcher、Engine Pack 构建/下载、Lite/Full、Payload 与旧版 Bundle 等全部 Portable CLI 入口统一将 stdout/stderr 切换为 UTF-8，并为不可编码字符保留回退表示，修复 Windows `cp1252` 控制台输出中文时直接崩溃或误报构建失败。
- **portable/native**: Windows Payload 改为在 Windows runner 构建，并以当前 Python ABI 的实际 `.pyd` 文件作为成功条件；禁止将 Linux `.so` 或旧 ABI 模块装入 Windows Portable，Full 离线冒烟会验证 C、Cython 与 Rust 后端均已加载。
- **native**: Cython 第二轮加速的时间戳和长度/索引统一使用双精度与 `Py_ssize_t`，修复 Unix epoch 分桶及长时间轴 SRT 与 Python fallback 不一致；Rust 构建改为实时显示 Cargo 输出。
- **subtitle**: `line_gap_ms` 现在按词间停顿阈值执行字幕断句，修复字幕模板配置已保存但不生效。
- **release**: Engine Pack CLI 在入口统一配置 UTF-8 输出并保留不可编码字符的回退表示，修复 Windows runner 使用 `cp1252` 代码页时 Fixture 构建因中文日志触发 `UnicodeEncodeError`。
- **login**: 系统 Chrome 与托管 Chromium 登录均显式启用 sandbox，并改为从独立 Playwright 上下文读取全部 Cookie 后按 Bilibili 域名边界筛选，修复新版 Chrome 下登录完成但无法捕获 Cookie 的问题。
- **web**: 修复候选片段拒绝请求的模板字符串未闭合导致前端 ES Module 初始化中断、页面按钮全部失效，并增加全量静态 JavaScript 语法回归检查。
- **portable**: Full 首次安装完成依赖后，`app.cli` 导入检查改为显式使用已安装的内容寻址 Runtime 源码，并在失败时保留原始 stdout/stderr。
- **portable**: 冻结 Launcher 的 Engine Pack、在线模型下载和模型校验入口改用绝对导入，修复 PyInstaller 顶层脚本缺少包上下文导致的模型准备崩溃。
- **release**: Full 离线 smoke 从实际 Payload 解压源码，在干净工作目录中使用 Full venv 导入 `app.cli`，并让冻结 EXE 使用 Fixture Engine Pack 完成模型准备。

- **portable**: PyInstaller 显式收集 Engine Pack Manifest 运行期依赖的 `model_catalog`、`version_loader` 及两份 JSON 配置，修复冻结 EXE 解压模型包后报 `ModuleNotFoundError`。
- **release**: Engine Pack 构建器同步生成 dist `engine-pack-info.json`，防止外部元数据残留旧版本；Full 构建清单补充 CRC32 并改为流式计算大文件哈希。
- **portable**: Engine Pack 内部 Manifest 统一使用安装器契约的 `format_version`，并让构建自校验调用真实 `load_manifest()`，避免“构建自检通过但首次安装失败”。

- **release**: Full 发布清单仅写入 `artifact_class=production` 的 Engine Pack CRC32，防止 CI 无模型构建误引用 Fixture 元数据。

- **cli**: 补齐 `python -m app.cli` 模块入口，修复 Portable Launcher 到达启动阶段后 Web 服务静默退出。

- **portable**: Launcher 显式调用 Typer `app.cli:app` 启动服务，不再依赖锁定 Payload 是否实现 `python -m app.cli`。

## V0.1.15.1 Alpha (2026-07-22)

### 变更

- **portable**: 账号登录优先调用系统已安装的 Google Chrome；不可用时复用或按需安装 Playwright Chromium，并补充相应状态提示与回归测试。
- **portable**: Full/Lite 运行时锁补齐 Playwright 依赖与安装导入冒烟检查，离线 wheelhouse 最低数量同步更新为 110。
- **docs**: 新增面向普通 Windows 用户的 Portable 从零使用说明，并同步下载、校验、浏览器和故障排查步骤。
- **repository**: 完善 Docker 构建上下文忽略项，修正 Docker 文档中的启动与停止脚本名称。

## V0.1.15 Alpha (2026-07-21)

### 变更

- **portable**: 固定源码基线升级至当前 `main` 的 `4bdaa13`，移除已被新基线原生吸收的历史 Backport，并增加 Payload 业务源码逐文件一致性回归测试
- **native**: PyO3/Rust 扩展构建显式使用当前虚拟环境的 Python，避免构建子进程找不到解释器后退回纯 Python 实现
