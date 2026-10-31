# v0.1.20.3 Alpha 可靠性修复实施清单

本次修复覆盖录制断流、数据库竞争、进程退出、崩溃恢复、模型调用和业务正确性。目标版本为 `v0.1.20.3-Alpha`，包版本为 `0.1.20.3-alpha`。初始代码基线为 `d88ca3a76f0935222e6bceb53e3d2ef123451840`。

先创建草稿 PR，再修改业务代码。所有阶段串行执行，每个阶段通过验收后，在同一个 PR 追加提交和中文进度评论。

## 阶段和验收

| 阶段 | 状态 | 工作项 | 验收条件 |
| --- | --- | --- | --- |
| S0 基线与恢复契约 | COMPLETED | 建立 PR、复现基线，明确状态、租约、取消和新增持久状态的兼容规则 | 问题证据及基线可复查，已有资产得到保留 |
| S1 安全与外部输入 | COMPLETED | M01 至 M06 | 认证、CSRF、合法上传、恶意输入和慢通知回归通过 |
| S2 数据库并发与事务 | COMPLETED | M07 至 M10 | 并发和锁竞争测试通过，租约、证据和清理互斥保持有效 |
| S3 生命周期与资源所有权 | COMPLETED | M11 至 M12 | 正常关闭、超时取消、异常终止均回收所属资源，不影响其他实例 |
| S4 录制断流恢复 | COMPLETED | M13 至 M15 | 短暂断流、连续短片、无进度、正常下播和人工暂停按状态处理 |
| S5 崩溃与持久断点恢复 | COMPLETED | M16 至 M22 | 关键提交点终止后可恢复，不丢失已确认结果，不重复外部副作用 |
| S6 LLM 与证据正确性 | IN_PROGRESS | M23 至 M28 | 保留大容量和真实性校验，各类故障分别可诊断、可恢复 |
| S7 路径与业务正确性 | PENDING | M29 至 M32 | 搬迁副本、审核到渲染、统计和端点恢复验证通过 |
| S8 版本与最终验收 | PENDING | 同步版本、内容和来源信息，完成最终工程及制品验收 | 最终源码状态的全部必需门禁通过 |

各代码阶段按目标测试、必需完整回归、Ruff 及其他质量检查、相关构建、内容一致性和实际 diff 的顺序验收。当前阶段验收通过前，不开始后续阶段的修改、测试或构建。阶段记录引用实际命令和结果；等待、失败和未执行均不能记录为通过。

## 修改项

| 编号 | 修改内容 | 原问题或指南编号 |
| --- | --- | --- |
| M01 | 消除默认审核员占位凭据，保留用户自定义配置 | P0-1 |
| M02 | 统一可信代理、来源、HTTPS scheme 和文档认证边界 | P0-2、P2-9 |
| M03 | 修复静态资源安全头，验证生产 CSP | P2-1 |
| M04 | 按路由限制请求体，严格校验模板类型和范围 | P2-2、P2-4 |
| M05 | 限制弹幕压缩帧、解压累计量、递归和消息数量 | P2-3 |
| M06 | 通知后台投递、HTML 转义和 Webhook 校验 | P1-10 |
| M07 | 事务遥测，消除锁内嵌套会话及重型操作 | P1-11 |
| M08 | 弹幕、心跳批量写入及背压，保留租约身份校验 | P1-11 |
| M09 | 审核调度批量查询、提前筛选和会话复用 | P1-4 |
| M10 | 日志容量、索引、保留、聚合和提交后日志 | 原10、P1-9、P1-11 |
| M11 | 在途取消、关闭总预算和物理退出 | P1-1 |
| M12 | 服务单实例及所属进程树管理 | 原13、P2-7 |
| M13 | 重连预算计时和稳定恢复判据 | 原2 |
| M14 | 同场冷却后恢复和真实失败状态 | 原1、原3 |
| M15 | 媒体进度 watchdog | 原4 |
| M16 | 运行意图、监督启动及恢复就绪 | 原11、原12、原14 |
| M17 | 尾片登记和旧场次收尾 | 原15 |
| M18 | 原始 ASR 和远端分块持久断点 | 原16 |
| M19 | 崩溃恢复的有限重试预算 | 原18 |
| M20 | 渲染制品验证和恢复接入 | 原19 |
| M21 | 上传心跳、持久回执及未知结果核对 | 原20、原21 |
| M22 | 页面后台任务终态恢复 | 原22 |
| M23 | 协议诊断、有限传输重试和费用提示 | P0-3 |
| M24 | 时间定位、数字、实体和引用依据正确性 | 原5、原6、原7、P0-3 |
| M25 | 稳定证据快照和事件级有限重算 | 原8、P1-2 |
| M26 | 请求身份、费用未知状态和失败结果补做 | 原9、原17 |
| M27 | 分层总结、块级恢复和增量缓存 | P1-6 |
| M28 | 出站代理环境一致性 | P1-8 |
| M29 | 统一媒体路径协议，支持后续整体搬迁 | P0-4 |
| M30 | 自动审核边界、人工批准和幂等提交 | P1-3、P2-5 |
| M31 | 分析页时间范围、SQL 条件和日期统计 | P1-5 |
| M32 | 标题刷新、端点健康和重复告警治理 | P2-10 |

## 保留的边界

- 保留现有 Portable、媒体、配置和历史证据；不在运行目录实施修复或数据迁移。
- 不自动重写历史路径，不恢复已撤回的历史数据兼容扩展。新增持久状态优先复用现有状态存储；必要格式变更须明确升级和恢复方式。
- 保留大上下文、大输出、证据真实性、过期结果拒绝和人工批准语义。
- 保留已存在的整场总结 claim、启动恢复和最多三次重试，补齐其余失败结果的持久补做。
- 保留未退出执行的资源占位和拒绝重复启动保护，补充最终物理退出机制。
- 不以扩大连接池、无界重试、丢弃业务弹幕或删除测试消除故障。
- 普通运行日志的保留策略不清除应保留的审计和历史故障记录。
- 不自动添加第二家模型服务，不追加已耗尽授权额度的 DeepSeek 请求。
- 通用数据库迁移框架、无关死代码清理、凭据加密和插件沙箱不在本次修复范围。
- 后续只追加 commit，禁止 amend、rebase、强制推送和改写已推送历史；本 PR 不包含合并、打标签或发布授权。

## 阶段记录

### S0 建立交付入口

本地 `main` 已通过快进同步至上述基线，`main...origin/main` 为 `0/0`。PR #99 先由实施清单建立，随后使用隔离工作树实施。主目录的旧配置、原开发环境和未跟踪资料保持原状。业务源码和版本尚未修改，第三方依赖按现有锁文件安装至专用环境。

恢复契约如下：

- 新运行意图、累计崩溃预算和块级 checkpoint 复用 `AppSetting` 的独立版本化 JSON 命名空间，不扩表，不经配置保存入口增加全局 `configuration_revision`。
- 不向严格校验的房间配置直接添加未知字段；人工暂停、永久错误抑制、临时断流和正常退出分别处理。
- 提交和批量心跳继续校验 `task_id + worker_id + lease_token + expected_stage`；失去租约的结果不进入新执行。
- 取消后保留执行占位，确认线程或所属进程物理退出后释放；服务单实例、任务租约和操作系统进程归属分别管理。
- 保留 Web 作业代次、结果事务、已有摘要 claim 及有限重试；已落盘上传成功回放，远端结果未知时进入核对状态。
- 进程内预算使用 monotonic，持久截止时间使用 UTC；记录 PID 不单独构成进程归属证据。

基线命令及结果：

1. `uv sync --locked` 在专用环境安装 118 项锁定依赖；原生 C、Cython 和 C++ 扩展均已构建。首次在主目录执行测试时，旧 `.env` 的七个已移除字段阻止收集；隔离工作树解决了配置混入，无需修改用户配置。
2. 恢复、租约、录制和生命周期目标测试：158 passed。
3. 首次完整基线：3847 passed、8 failed、41 skipped；失败及跳过由缺少本地 Payload 前置制品引起。正式 `python packaging/portable/build_payload.py` 保留源码冻结和两次可重复构建，补齐前置制品后，Portable 集合 471 passed。
4. `python -m pytest tests packaging/portable/tests --cov=app --cov-report=xml --cov-fail-under=50 --timeout=600 --fail-on-skip` 复验：3896 passed，覆盖率 84.98%。600 秒覆盖既有真实 wheel 构建的预算，未删除用例或放宽断言。
5. `python -m pre_commit run --all-files --show-diff-on-failure` 全部通过，包含 Ruff、格式、版本、快速发布审计、架构和 React 检查。
6. 初始文档提交的远端 19 项检查全部结束：18 success，文档部署按 PR 条件 skipped。
7. `python scripts/release_audit.py`：63 PASS、0 WARN、0 FAIL；实际 diff 与空白检查通过，只有本实施文档变化。

后续涉及 Payload 内容的阶段，在源码提交后更新来源元数据，再执行正式构建；不得沿用旧 source pin 构建，或绕过工作区源码冻结检查。

### S1 安全与外部输入验收完成

M01 至 M06 已实施：默认审核员为空并拒绝占位凭据；认证和限流使用可信来源；独立前端支持显式可信代理；静态资源发送 CSP 和安全头；请求体按实际路由限制；模板使用严格部分更新和原子导入；弹幕共用解压、消息和深度预算；通知复用有界后台队列并保留邮件验证与 HTML 转义。

合法视频与弹幕上传分别保留 50 GiB、32 MiB。转写和房间编辑保留 4 MiB、模型目录保存及测试保留 8 MiB，容纳已有字段容量和 Unicode JSON 转义；其他管理请求为 2 MiB。Docker 外层 TLS 代理的专用可信配置见部署说明，默认镜像继续按直接 HTTP 入口处理。

本地冻结源码为 `894fc6410c9e2ad43ef972a969ecc23dcc191d00`，版本保持 `0.1.20.2-alpha`，在 S8 统一升级。Portable 来源常量同步至该提交，模型 revision 与第三方依赖锁不变；Engine Pack 资源元数据由正式 fixture 构建生成，fixture 不作为发布模型包。

实际验收：

1. 安全、代理、模板、通知及配置目标集：382 passed；补齐弹幕既有邻居消息行为后，协议回归 63 passed，身份与审计回归 27 passed。
2. `python -m pytest tests packaging/portable/tests --cov=app --cov-report=xml --cov-fail-under=50 --timeout=600 --fail-on-skip --durations=15`：3970 passed，覆盖率 85.21%，包含真实 sdist、wheel、双端启动与全部 Portable 用例。
3. `python -m pre_commit run --all-files --show-diff-on-failure` 全部通过；`npm --prefix frontend run check` 全部通过，含 76 项单元测试和生产构建；`python scripts/export_openapi.py --check` 通过。Python 未配置独立类型检查器，TypeScript 检查按项目配置执行。
4. `python packaging/portable/build_engine_pack.py --fixture` 自校验通过；`python packaging/portable/build_payload.py` 保留固定源码和默认两次可重复构建，摘要一致。`python scripts/release_audit.py`：63 PASS、0 WARN、0 FAIL。
5. `npm --prefix docs audit --include=dev --audit-level=low` 无漏洞；文档检查 22 passed；构建 57 页并通过全部生成链接检查。

远端验收覆盖提交 `91c16f4a6761a855de192b8be3b899b86da7ef31`，五项工作流均在 attempt 1 成功结束：CI `40997622259`、Docker `40997622301`、Frontend `40997622271`、Documentation `40997622233`、CodeQL `40997622221`。CI 的全部 15 个 job（含完整矩阵、Portable、coverage summary、CI status）通过；23 项 commit checks 中 22 项成功，Pages 部署按 PR 条件跳过，无 legacy statuses。

本机没有 Docker，已通过远端 Docker 的真实容器和 Chromium 验收，新增生产 CSP、Service Worker、静态资源缓存及登录流程断言全部执行成功。Frontend 真实桌面与手机浏览器、Documentation 的 Windows 和 Ubuntu 构建也通过。实际 diff 未包含运行配置、密钥、大型制品或其他工作树的修改。S1 为 `COMPLETED`，后续按顺序进入 S2。

### S2 数据库并发与事务验收完成

S2 已完成 M07 至 M10，S3 至 S8 保持 `PENDING`。

- 事务遥测记录连接进出、显式写锁等待、提交及根事务实际结果，保留最近 512 次事务的 P95 和最大耗时。超过 500 ms 的事务输出诊断，超过 2 s 使用 ERROR；遥测不进入数据库日志队列。
- 媒体文件预检在会话关闭后执行，提交事务核对原路径、边界及数据库来源身份。预检仅覆盖对应提交事务，重新计算时读取实时来源。清理使用跨进程独占媒体门，引用变更与使用媒体的操作使用共享门；文件删除期间不占业务数据库事务。响应的媒体门覆盖实际读取及发送，取消后释放。
- 公共归一化弹幕满 100 条或每 1 s 批写，状态切换和停止刷新尾批；提交成功后才推进已确认范围。Bilibili 原有逐帧批写与全量原始归档、数据库采样语义保持有效。
- 任务心跳共用调度线程，单批最多 64 项，保持原 5 s 周期和首次及时更新。每项继续校验任务、worker、token 和期望阶段；停止等待已发出的提交，防止清除后被旧批次重新续租。
- 等待审核使用原因聚合和就绪投影；全部关闭自动批准时只执行一条只读查询。就绪项目在媒体预检后重新核对批准条件，同一事务内复用会话。
- 数据库诊断副本最多容纳 512 条（含在途记录），每批最多 100 条。慢数据库或容量耗尽时保留文件日志并汇总未保留副本数，停止时补充最后一次汇总。仅新标记的普通 WARNING 按 14 天或 20000 条保留；旧行、业务审计与 ERROR 不按该策略清除。三个普通索引在完整 Schema 校验后幂等创建，没有更改数据表或 Schema 指纹。
- 候选、事件、批准及调度状态的成功日志在外层事务提交后输出，回滚和 SAVEPOINT 回滚丢弃相应日志。

本地源码冻结提交为 `ac65279d6f85164c51da8b55c587e8b6c53ef706`，Portable 来源信息已同步；版本、模型 revision 和第三方依赖锁保持原值。原有诊断测试已改为复用正式数据库 sink，同时覆盖日志初始化前后两种状态，保留全部脱敏和异常链断言，并核对恰好保存一条记录。

实际本地验收：

1. 数据库、媒体、调度和录制目标集：381 passed；诊断接入修复组合：46 passed；最终诊断目标：25 passed。
2. `python -m pytest tests packaging/portable/tests --cov=app --cov-report=xml --cov-fail-under=50 --timeout=600 --fail-on-skip --durations=15`：4011 passed，覆盖率 85.44%，包含真实 wheel、媒体、独立前端启动和全部 Portable 测试。
3. `python -m pre_commit run --all-files --show-diff-on-failure` 全部通过。`npm --prefix frontend run check` 通过类型、lint、格式、76 项测试和生产构建；`python scripts/export_openapi.py --check` 通过。Python 沿用现有 Ruff，未配置独立类型检查器。
4. `python packaging/portable/build_engine_pack.py --fixture` 完成真实 ZIP 校验；`python packaging/portable/build_payload.py` 保留固定源码和默认两次可重复构建。Payload 包含 308 个文件，SHA-256 为 `b87f434cbb647c3898014fbef0b344e6f529fc59c07338191fe29106a90bfa6a`，大小 1487337 字节，CRC32 为 `F56BC59C`。Engine Pack 为本地 fixture，未作为真实模型包发布。
5. `python scripts/release_audit.py`：63 PASS、0 WARN、0 FAIL。制品与来源信息、ZIP 成员 CRC、非分发内容检查通过；本地生成清单记录了两个 ZIP 的版本、大小、SHA-256 和 CRC32。
6. `npm --prefix docs run check`：22 passed；`npm --prefix docs run build`：57 页及全部生成链接检查通过。实际 diff 仅包含本阶段源码、目标测试、来源信息和本记录，没有运行配置、密钥、大型制品或其他工作树的修改。

远端首次应用矩阵使用 Starlette 1.10.0，暴露了新增传输测试的接收边界错误：`receive()` 会即时、重复返回空请求，新版断开监听因此忙循环。已依照 [ASGI 请求与断开协议](https://asgi.readthedocs.io/en/latest/specs/www.html)改为一次完整请求，后续阻塞等待输入；原有实际发送、取消及媒体门断言完整保留，并分别覆盖 ASGI 2.0、2.4。书面告知后，在专用验证目录安装[官方 Starlette 1.10.0](https://pypi.org/project/starlette/1.10.0/)复现旧测试的超时；修复后该版本与项目锁定的 1.7.0 各通过 40 项媒体及审核回归。项目依赖锁、超时设置、生产代码和既有 Payload 的来源保持有效。

远端验收覆盖修正后的提交 `4e736a90c40dcecc7d14c8c5fb463210d7b42843`，五项工作流均在 attempt 1 成功结束：CI `41017646533`、Docker `41017647134`、Frontend `41017646607`、Documentation `41017646630`、CodeQL `41017646627`。CI 的全部 15 个 job 通过，包含 Linux、Windows、macOS 的七个应用矩阵、三个 uv 安装与原生验证、Portable、依赖审计、coverage summary 和 CI status。23 项 commit checks 中 22 项成功，Pages 部署按 PR 条件跳过，无 legacy statuses。

本地最终验收与远端验收均通过，S2 为 `COMPLETED`。本次完成提交仅补充阶段记录，执行适用的文档检查、构建、共享质量门禁和实际 diff 检查；应用完整集及 Payload 构建结果继续覆盖未变化的源码，不重复执行。S3 至 S8 保持 `PENDING`，后续按顺序进入 S3。

### S3 生命周期与资源所有权验收完成

本阶段实施 M11、M12，依赖的 S2 已完成本地与远端验收。以下保留实施过程及各次验收记录，最终结果见本节末尾。

工作项目：

1. 将全局关闭与任务或作业取消意图传入模型请求、等待及资源清理，阻止取消后重试和迟到提交；保留已有大容量与请求时间配置。
2. 所有关闭步骤共用剩余预算，先通知停止接单，再回收网络、ASR 和媒体执行；未确认物理退出时保留执行与资源占位，输出可定位诊断。
3. 应用启动前获取实例所有权，端口占用时在安装前退出；启动器和服务只回收自己创建的进程树，覆盖正常关闭、异常退出及父进程终止。

验收适用性：生命周期、模型外部 HTTP 边界、租约、真实子进程及 Portable 启动目标测试；跨模块完整应用与 Portable 回归；全部 pre-commit、前端类型与质量检查；正式 Payload、涉及的启动器制品和完整发布审计；文档、来源信息与实际 diff。初始状态参考 S2 最终 4011 项本地通过及七平台远端矩阵，并在修改前运行本阶段目标基线。测试不得把取消等待等同于底层退出，也不得把不属于本实例的进程作为回收目标。

已实施共享取消、同步 SDK 可取消网络传输、关闭总期限、服务实例锁、Launcher/API/前端及录制所属进程树。正式结果提交前复核关闭与执行身份；取消后的草稿清理失败保留原取消异常及清理原因。POSIX 父死亡监管保留真实命令返回码，Windows 使用创建时的 Job 所有权；外部模型安装器随包携带共享锁模块。当前仍在验收，尚未声明阶段完成。

366 项初次目标回归通过后，完整集发现 13 项录制测试失败：外部进程替身缺少新增监管协议，导致必需辅助任务、竞争录制及永久错误分类提前退出。已补齐 OS 创建、管道回收与 Job/进程组边界，保留全部业务断言。随后 181 项目标集仅剩一项 Windows 满管道测试输入不充分，改为小块写入后，7 项真实进程测试全部通过，覆盖 stderr 关闭、写满、父进程异常终止与无关进程保留。完整回归和构建门禁继续执行。

修正后的目标集为 182 passed，完整应用与 Portable 集为 4057 passed，覆盖率 85.29%；全部 pre-commit 检查通过。随后修正未使用循环变量并补充测试辅助文件白名单，CLI 与真实关闭复验 59 passed。业务源码保存为 `564f2bf7ca52faea2c659d39287b077802d60c51`；五处 Portable 来源信息已同步，依赖版本和模型 revision 不变。

默认 Payload 两次构建一致，5 个 bootstrap wheel 均通过官方来源及锁定哈希校验。使用 PyInstaller 6.22.3、hooks-contrib 2026.9、altgraph 0.17.5、pefile 2024.8.26 构建真实 Lite EXE，采用正式不内嵌模型包模式。全新隔离目录通过冻结 EXE 离线安装、`pip check`、Runtime 校验、四引擎 fixture 模型准备、Web 与设置 HTTP 检查；重复启动在安装前退出。强制结束实际冻结宿主后，所属服务全部退出，独立哨兵进程仍运行。fixture 仅用于本地验证，未发布模型或程序。

来源元数据更新后的最终本地验收已通过：

1. `python -m pytest tests packaging/portable/tests --timeout=600 --fail-on-skip --cov=app --cov-report=term --cov-report=xml --cov-fail-under=50 --durations=15`：4057 passed，覆盖率 85.29%，包含真实 wheel 与 Portable；唯一运行期 warning 为 setuptools 的旧安装接口弃用提示。
2. `python -m pre_commit run --all-files --show-diff-on-failure` 全部通过；`npm --prefix frontend run check` 包含类型、lint、格式、76 项测试与生产构建，全部通过；`python scripts/export_openapi.py --check` 通过。
3. `python scripts/release_audit.py`：63 PASS、0 WARN、0 FAIL；`npm --prefix docs run check` 为 22 passed，文档构建完成 57 页并通过全部生成链接检查。
4. Payload、fixture ZIP 和实际 Lite EXE 的来源、大小、SHA-256、CRC32、许可证及内容校验通过。EXE 内嵌 Payload 和共享锁源文件与输入完全一致，1950 个冻结 Python 模块及其嵌套代码对象未包含绝对源码路径。未提交本地运行配置、下载缓存或大型制品。

本地验收记录与来源元数据将追加到同一 PR。S3 仍为 `IN_PROGRESS`，等待远端全部适用 CI；Windows 本地结果不代替 POSIX 真实进程验收。S4 至 S8 保持 `PENDING`。

提交 `4a2f525` 的 Ubuntu / Python 3.12 首次远端验收出现三项失败，3582 项通过，覆盖率 85.20%；Python 3.11 与 macOS 出现相同三项失败。两个录制 EOF 用例在替换外部命令时移除了 POSIX 监管器，诊断用例的 `Popen` 替身未返回启动握手，均在原业务断言之前失败。已将真实 EOF 子进程保留在正式监管链中，并补齐诊断测试的 OS 握手和管道清理；重连次数、签名地址刷新、错误状态、取消异常、清理占位与脱敏断言保持不变。修正目标 55 passed，本地完整集 4057 passed。

同次 Ubuntu / Python 3.13 检查还暴露了启动握手的生产缺陷：合法文件描述符编号超过 `select()` 容量时抛出 `ValueError`，引发 109 项失败。握手改用 Python 标准库 `selectors.DefaultSelector`，保留原期限、EOF、容量及真实 exec 错误检查。新增隔离进程回归，真实占用低编号描述符后重复启动同步/异步命令，核对输出、缺失命令错误及每轮描述符数量。Windows 三个矩阵、Portable、uv、Docker、Frontend、CodeQL 与 Documentation 已通过旧 HEAD；该结果不能代替新修正的验收。S3 保持 `IN_PROGRESS`。

最终修正源码为 `23b4fd8dbe8755e9e89fd083638b1ff0824dcb4a`，五处 Portable 来源与正式生成的 Engine Pack 元数据已同步。目标回归 68 passed，Portable 与版本目标 491 passed；上述完整应用与 Portable 命令在最终来源状态得到 4058 passed、覆盖率 85.28%。全部 pre-commit、OpenAPI、文档 22 项测试及 57 页构建通过，完整发布审计为 63 PASS、0 WARN、0 FAIL。默认 Payload 可重复构建和真实 Lite EXE 重新构建、内容检查通过；新 EXE 在本任务隔离目录完成离线更新、运行时与 HTTP 验收、重复启动拦截及异常宿主退出后的进程回收，原全新离线安装证据保留。

最终 Payload 大小 1503221 字节，SHA-256 为 `035bd942f4c6ba0583e367b43eefe03ba8358819ff8a52f0744057b3081dc3ee`，CRC32 为 `A8BCED21`；Lite EXE 大小 67116282 字节，SHA-256 为 `f2b72fe12440c16c54280c02131ff66a91e389859eb8aa2ecb8cc8eab4f118c7`，CRC32 为 `03470D28`。两者仍为 `0.1.20.2-alpha`，版本统一更新安排在 S8。

远端验收覆盖提交 `5070425ed3885e23be0dd3ed91446933a29c9160`：CI `41054350186`、Docker `41054349730`、Frontend `41054349771`、Documentation `41054350335`、CodeQL `41054349723` 均在 attempt 1 成功结束。CI 全部 15 个 job 成功，包含七个应用矩阵、三个 uv 验证、Portable、审计、coverage summary 和 CI status；23 项 commit checks 中 22 项成功，Pages 部署按 PR 条件跳过，无 legacy statuses。Ubuntu / Python 3.13 最终为 3587 passed、覆盖率 85.17%，真实高编号描述符和原失败路径均已执行。

S3 的本地及远端验收全部通过，状态为 `COMPLETED`，S4 至 S8 保持 `PENDING`。本次完成记录仅修改文档，执行文档检查、共享质量门禁、文档构建与实际 diff 检查；完整应用、前端和制品结果继续覆盖未变化的源码。

### S4 录制断流恢复

S4 为 `IN_PROGRESS`，实施 M13 至 M15，S5 至 S8 保持 `PENDING`。基线为 S3 完成提交 `aa155eb4e69ec23f9eade7521ed9aa504ae30553`。

工作项目：

1. 从实际取流尝试开始累计连续故障预算，将取流、候选竞争和等待纳入同一期限；稳定媒体恢复及时重置，连续短片不清空预算。
2. 将临时故障后的冷却恢复与人工暂停、永久错误、正常下播分别处理，保留本场失败原因，在平台仍确认直播且冷却结束后经过原启动闸门恢复。
3. 根据实际媒体推进监测存活但无产出的录制进程，终止所属进程后进入有限重连，正常长分片、停止和下播不会被误报。

验收适用性：录制预算、真实 FFmpeg 与外部故障边界、候选竞争、事件和轮询监控、人工控制及页面状态目标测试；完整应用与 Portable 回归；全部 pre-commit、前端类型及质量检查、OpenAPI 一致性；正式 Payload、真实 wheel 和完整发布审计；相关文档、来源及实际 diff。本阶段不修改 Launcher 打包方式，EXE 的最终版本构建在 S8 统一执行。当前修改前先执行已有录制及监控目标基线；S5 的持久恢复工作不提前实施。

已实施有限故障预算、同场冷却复核、真实失败状态和媒体进度监控。恢复身份覆盖异步平台查询、资料刷新和新事件；迟到的旧结果不覆盖人工操作或下一次录制。普通停止、显式暂停、强制停止与永久故障分别显示。冷却状态目前仅在进程内保存，跨进程恢复按 S5 的已确认契约实施。

修改前目标基线 226 passed。最终录制、来源、事件监控、控制和真实子进程目标集 263 passed；页面状态回归 9 passed；OpenAPI 一致性通过。新增外部进程测试保留正式所属进程监管，覆盖无媒体推进、仅时间变化、其他尝试输出、健康长分片、首轮期限、强故障分类、稳定登记、退出后的尾片和重复取消。测试暴露的提前显示录制状态已修复，重连必须达到稳定媒体条件后才报告恢复。

下一步以新增本地提交固定正式 Payload 的源码输入，再同步来源元数据并准备完整回归所需制品。S4 保持 `IN_PROGRESS`，完整回归、质量、相关构建及最新提交的远端验收均须完成后，才能进入 S5。

S4 冻结源码为 `d1a5460ff53ba1ce3705f084d81d005f6b3c146f`，五处来源常量与正式生成的 Engine Pack 元数据已同步。版本仍为 `0.1.20.2-alpha`，模型 revision、依赖锁和既有运行目录不变。最终本地验收：

1. `python -m pytest tests packaging/portable/tests --timeout=600 --fail-on-skip --cov=app --cov-report=term --cov-report=xml --cov-fail-under=50 --durations=15 -o addopts=--tb=short -q`：4082 passed，覆盖率 85.32%，包含真实 sdist/wheel、原生组件和 Portable 回归；保留一条 setuptools 安装接口弃用 warning。
2. `python -m pre_commit run --all-files --show-diff-on-failure` 全部通过；`npm --prefix frontend run check` 通过类型、lint、格式、85 项测试及生产构建；`python scripts/export_openapi.py --check` 通过。Python 沿用项目 Ruff，未配置独立类型检查器。
3. `python packaging/portable/build_engine_pack.py --fixture` 自校验通过；`python packaging/portable/build_payload.py` 保留源码冻结及默认两次可重复构建。最终 Payload 为 1505708 字节，SHA-256 `db8598942eab009609786f68d8076527b9ec81d1e61951b4d5b9e0c66cfd7ea7`，CRC32 `081A995F`。逐文件内容、来源、ZIP CRC 和非分发内容检查通过；Engine Pack 明确为测试 fixture。
4. `python scripts/release_audit.py`：63 PASS、0 WARN、0 FAIL。文档 `npm --prefix docs run check` 为 22 passed，`npm --prefix docs run build` 为 57 页并通过全部生成链接检查。

本地验收通过，S4 保持 `IN_PROGRESS`，等待最新提交的全部适用远端 CI。没有新增依赖版本、模型 API 请求或发布操作。S5 至 S8 保持 `PENDING`。

最终远端验收覆盖 `6fc775dc00798c447dbe010dc3d1d87d0f9aa050`：CI `41068679914`、Docker `41068679979`、Frontend `41068679915`、Documentation `41068679935`、CodeQL `41068679911` 均在 attempt 1 成功结束。CI 的全部 15 个 job 成功，包含七个应用矩阵、三个 uv 验证、Portable、审计、coverage summary 和 CI status。23 项 commit checks 中 22 项成功，仅 Pages 按 PR 条件跳过，无 legacy statuses。Ubuntu / Python 3.13 为 3611 passed、覆盖率 85.22%，新增媒体停写和取消清理回归已在 POSIX 执行；该环境输出的 SQLite ResourceWarning 等告警保留在原日志中。

S4 本地及远端验收通过，状态为 `COMPLETED`。本次完成记录仅修改文档，执行适用的文档、共享质量、构建和实际 diff 检查；业务源码和制品的有效验收结果保持不变。S5 至 S8 保持 `PENDING`，随后按顺序进入 S5。

### S5 崩溃与持久断点恢复

S5 为 `IN_PROGRESS`，基线为 S4 完成提交 `89e6839570204f920cc7d7f4f0314ca19805ebad`。S6 至 S8 保持 `PENDING`。本阶段沿用 S0 的版本化 AppSetting、任务租约、进程所有权和结果回放契约，不增加通用迁移框架或现场历史数据修复。

工作项目和依赖：

1. M16：持久保存录制运行意图及人工停止边界；监督入口在异常子进程退出后按有限预算恢复，服务就绪须等待旧执行清算完成。保留授权、自动开关、来源和实例所有权检查。
2. M17：启动清算旧会话的已落盘尾片和清单，验证真实媒体后幂等登记，再结束旧场次；未确认写入完成的文件不得伪造可用结果。
3. M18：在 ASR 原始结果与后处理之间、远端转写块之间保存可核验断点；按输入、配置及模型身份恢复，最终业务提交继续受租约保护。
4. M19：对进程崩溃和失联执行累计持久恢复预算，正常成功与显式用户重试分别处理；服务重启不能重置连续失败次数并形成无界重试。
5. M20：核验渲染制品的身份、完整性及实际媒体属性，恢复时接入已有提交链，不以文件存在代替渲染成功。
6. M21：上传执行维持当前 attempt 的心跳，远端成功先持久保存可回放回执；结果未知保持核对状态，禁止自动重复上传。
7. M22：页面后台作业依据已确认的业务结果恢复终态，保留作业与尝试代次、权限及取消语义，不重新执行已经完成的外部副作用。

验收适用性：在现有录制恢复、ASR、任务租约、发布回放、渲染提交和后台作业目标集上记录修改前基线；新增真实进程中断与提交边界回归，模拟范围仅限外部网络、时钟、进程或受控数据库故障。跨模块完整应用与 Portable 回归、全部 pre-commit、前端与文档检查、正式 Payload、相关监督启动器制品及发布审计均为本阶段门禁，最后检查内容与实际 diff。每个检查串行执行，只由主执行者修改；本地通过后在同一 PR 追加提交和评论，完成全部适用跨平台验收后才进入 S6。

修改前目标基线为 410 passed。已接入原始 ASR 与远端分块断点、持久崩溃预算、上传心跳与成功回执、页面上传作业终态恢复。ASR 目标集 92 passed；身份与回执补充组合 46 passed；上传和页面作业组合 90 passed。真实本地 HTTP 与 ASR 子进程中断验证了仅请求未确认块，上传测试只替换外部命令边界。模型请求授权、完整任务租约、人工取消、发布代次及冲突回执保护继续保留。

渲染断点已接入 Worker 启动预检和新租约的正式提交链。产物必须通过真实媒体属性、完整内容摘要、封面和 generation 校验；来源指纹覆盖实际编码选项、素材文件身份、字幕模板和带场次延迟的有界弹幕输入。真实 FFmpeg 产物移动后进程直接退出，再经过 stale 恢复、重新领取和正式提交，只登记一个成片，不重复渲染。渲染与终态组合 82 passed，字幕和弹幕输入组合 78 passed。

上传发送前增加持久执行来源与完整请求身份复验，只有匹配的 TaskLease、JobAttempt 或原直接调用才能发出同代次请求。真实成功记录不受迟到失败覆盖；已发送命令的心跳在物理退出前保留占用。上传和渲染组合 109 passed；真实本地命令成功后进程直接退出、回执幂等回放、SQLite 写锁期间心跳关闭等待等补充 25 passed。未请求外部上传平台。

录制意图和执行证明分别存入版本化 AppSetting。人工停止先持久撤销，预约接受与完成标记同事务提交；服务关闭保留可恢复意图，永久错误和初始化失败要求人工处理。旧代次无法覆盖新请求。FFmpeg 继承实际 writer 文件占用，清算必须确认最后一个 writer 退出；只登记已接纳线路中的真实可用媒体，损坏文件保留并记录原因。末片登记、旧场次终态与结束请求通过正式事务幂等完成，再开放服务就绪与调度。

独立 console 或 Portable Launcher 各由唯一监督者持有恢复预算，最多追加三次启动，连续就绪一分钟后才重置。上一代清理失败明确禁止重启，人工关闭保留实际退出结果；具体监听地址用于真实 HTTP 就绪检查。监督目标集 81 passed，录制持久恢复、预约及终态边界目标集 139 passed，包含真实 FFmpeg 宿主中断与尾片恢复。原失败记录和修正依据保留在本地验收日志中。

启动就绪屏障及相关录制回归为 86 passed。完整受影响目标组合首次为 350 passed、1 failed，原因是旧 Launcher 测试未恢复工作目录；补齐测试隔离后，两项连续复验通过。另用真实媒体复现并修复了启动清算重复探测已登记分片的问题：恢复只对尚未登记的尾片执行探测，writer、路径、文件身份和事务内幂等校验保持有效，相关 28 项回归通过。业务源码固定为 `241b2046678831c83dd7285c79e8c74e8f56163a`，默认 Payload 双次构建一致。

首次完整应用与 Portable 回归为 4161 passed、11 failed，覆盖率 85.54%。失败涉及旧测试绕过录制 writer 和持久执行初始化、自动开播替身与 ASR 替身的接口变化，以及 wheel 验收以终止前端请求关闭而触发新的自动恢复。已补齐正式生命周期，自动开播改用真实 Recorder 和真实媒体，仅控制外部传输与进程；ASR 错误在真实 HTTP 边界产生；wheel 使用父管道 EOF 正常关闭并要求退出码为零。录制及 ASR 复验 19 passed，原真实 wheel 独立启动和退出复验通过。没有降低业务断言或移除生产校验。

修正后的完整集合为 4172 passed、覆盖率 85.57%；全部 pre-commit、前端 85 项测试及生产构建、OpenAPI、文档 22 项测试与 57 页构建通过，完整发布审计为 63 PASS、0 WARN、0 FAIL。新 EXE 的内容与校验和、全新隔离目录离线安装、`pip check` 和 Runtime 校验通过。

实际冻结启动进一步发现：新增 urllib 就绪探测会先构造默认 HTTPS 处理器，触发构建环境 Python 的额外 TLS 数据文件依赖，在启动服务前退出。检查目标固定为回环 HTTP，现使用标准库 `HTTPConnection` 直接检查，保留 1 秒超时、HTTP 200 判定、连接关闭和既有恢复预算。真实 HTTP 状态、连接拒绝、TLS 初始化不可用及环境代理边界回归与监督组合为 73 passed；该修正需要重新完成最终回归和冻结验收。

最终本地验收已通过：

1. 完整应用与 Portable 命令保持 `python -m pytest tests packaging/portable/tests --timeout=600 --fail-on-skip --cov=app --cov-report=term --cov-report=xml --cov-fail-under=50 --durations=15 -o addopts=--tb=short -q`，结果为 4176 passed、覆盖率 85.54%，包含真实 sdist/wheel；保留一条 setuptools 安装接口弃用 warning。
2. `python -m pre_commit run --all-files --show-diff-on-failure` 全部 12 项通过；前端类型、lint、格式、85 项测试与生产构建、OpenAPI 检查通过。最终文档检查为 22 passed，构建完成 57 页及全部生成链接检查；完整发布审计为 63 PASS、0 WARN、0 FAIL。
3. 冻结启动器修正保存为 `3c3fe51`，使用正式 `python packaging/portable/build_exe.py --without-engine-pack` 重建。Payload 保持已验证业务来源 `241b204`；构建清单中的 `builder_commit` 沿用 Payload 构建来源，实际启动器构建提交单独记录为 `3c3fe51`。新 EXE 为 67151305 字节，SHA-256 `1f20f1829f69c887ca1d81ce194645265f2265f1124c27352aca07b2c5d2aeed`，CRC32 `CA1D09BD`；1951 个冻结模块及其嵌套代码对象不含绝对源码路径。
4. 全新隔离目录的离线安装、依赖检查、Runtime 校验和四引擎 fixture 准备证据保留。更新后的真实 EXE 分别经历后端、前端、主控制进程异常终止，均恢复唯一新服务代次并清除旧树，HTTP 与设置检查通过。最后终止实际冻结宿主，所属服务及端口全部回收，无关哨兵进程保持运行。

S5 仍为 `IN_PROGRESS`，等待最新提交的全部适用远端阶段验收；S6 至 S8 保持 `PENDING`。没有新增实际模型服务调用、依赖升级、版本发布或运行目录修改。

首轮远端提交 `a99a4d1` 的七个应用矩阵暴露测试进程枚举竞态：服务被终止后，读取旧子进程命令行可能得到 `NoSuchProcess` 或 `ZombieProcess`。Ubuntu 三个矩阵均为四项相同失败、3691 项通过；macOS 还确认前端绑定未配置的 `127.0.0.2` 返回 `Errno 49`。现将进程快照读取限制在精确退出异常范围，使用 PID 与创建时间区分代次，并要求后端与前端各有一个实际服务叶节点；旧服务完整后代保留供退出验证，包含 POSIX guardian。macOS CI 显式准备回环别名，测试开始和结束均验证实际监听地址，保留全部用例与原 HTTP、退出及资源回收断言。修正后相关目标 65 passed，完整应用与 Portable 回归为 4176 passed、覆盖率 85.54%。本次仅修改测试、CI 前置条件和验收记录；业务源码与冻结制品未变。更新后的远端门禁继续执行。

随后提交 `2f18cba` 的 Windows Python 3.12 完整集合为 3690 passed、1 failed，端口占用测试超过原有 30 秒期限；其余六个应用矩阵及 Docker、Frontend、Documentation、CodeQL 均成功。修改前本地真实端口占用确认启动了四代 API，原断言运行 29.49 秒；新增单代断言在旧生产代码上实际失败，记录四次启动。现仅在真实监听绑定边界分类地址占用、权限不足及地址不可用，保留 POSIX/Winsock 错误及原异常链；内部退出码 `78` 经 console/Launcher 清理后映射为公开失败码 `1`，不消费崩溃恢复预算。前端 `78` 优先于同轮后端退出，清理未确认的 `75` 继续优先；其他异常仍使用原有限恢复。

本次目标命令 `python -m pytest tests/unit/test_frontend_server.py tests/unit/test_cli_basic.py tests/unit/test_service_restart.py tests/integration/test_frontend_console.py packaging/portable/tests/test_launcher_entrypoint.py --timeout=120 --fail-on-skip --durations=10 -o addopts=--tb=short -q` 为 129 passed。真实 console、委托监督和 Launcher 端口占用均只启动一代且清理 API；原 30 秒期限未变。本次生产源码及 Launcher 均有变化，须重新固定 Payload 来源、完成完整回归和冻结制品验收后再推送，S5 保持 `IN_PROGRESS`。

绑定失败修复已固定为业务来源 `e0eadee`，五处来源引用及正式 fixture 元数据同步后，默认 Payload 双次构建一致。上述完整应用与 Portable 命令重新通过 **4187 项测试，覆盖率 85.60%**，包含真实 wheel；全部 pre-commit、OpenAPI、文档 22 项测试与 57 页构建通过，完整发布审计为 **63 PASS / 0 WARN / 0 FAIL**。正式新 EXE 为 67151532 字节，SHA-256 `0ded1fce18ce6c76ba8a92a9885e2a49e9e09dadbea5527e014c9a746d5aeeb2`，CRC32 `6AC9EB41`；内嵌 Payload、LICENSE、来源及 1951 个冻结模块的路径检查通过。

新冻结 EXE 在隔离 Portable 副本中离线更新，真实后端、前端和 console 分别异常终止后均恢复唯一完整新代，旧树和旧 API 端口回收，宿主退出不影响无关进程。另在 Launcher 通过端口预检、console 已启动后实际抢占前端端口，确认只启动一代 API、无恢复重试、公开退出码 `1`，全部已登记所属进程和 API 端口关闭。完整本地门禁已通过；S5 继续等待该修复的全部适用远端 CI，S6 至 S8 尚未开始。

提交 `0122ebe` 的 macOS 进程专项为 64 passed、1 failed：切换服务代次时，psutil 的 `sysctl(KERN_PROCARGS2)` 返回 `EINVAL` 并映射为 `AccessDenied`。新增受控读取故障用例在原轮询中实际复现失败。快照辅助函数继续拒绝返回部分结果，仅在既有 40 秒恢复轮询边界允许下一轮重新读取；持续权限错误仍在原期限内失败并保留诊断。成功仍要求完整快照、HTTP 200、两类服务各唯一、旧新身份不相交，旧树及 POSIX guardian 的回收断言保持有效。

本次仅调整测试及验收记录。进程目标命令 `python -m pytest tests/unit/test_process_tree.py tests/unit/test_process_control.py tests/unit/test_service_shutdown.py tests/integration/test_frontend_console.py --timeout=120 --fail-on-skip --durations=10 -o addopts=--tb=short -q` 为 **67 passed**；格式固定后两项新增回归再次通过。完整应用与 Portable 命令为 **4189 passed、覆盖率 85.60%**，保留一条 setuptools 安装接口弃用 warning。业务来源 `e0eadee` 及已验证制品未变化，继续使用原冻结验收证据；S5 远端验收仍在进行，S6 至 S8 保持 `PENDING`。

最终远端验收覆盖 `dbd5727ffa7da6c7e4cd03d99cc5407c8ef5ee6d`：CI `41127143615`、Docker `41127143669`、Frontend `41127143748`、Documentation `41127143651`、CodeQL `41127143602` 均在 attempt 1 成功结束。CI 全部 15 个 job 成功，包含七个应用矩阵、三个 uv 验证、Portable、审计、coverage summary 和 CI status；23 项 commit checks 中 22 项成功，Pages 部署按 PR 条件跳过，无 legacy statuses。macOS 原失败专项及其完整集合均实际执行成功。上一提交的文档缓存出口限制取消保留为历史，本次两平台文档构建均通过，没有重跑或关闭检查。

S5 的本地、冻结制品和远端验收均已通过，状态为 `COMPLETED`。本次完成记录只修改文档，执行文档检查、共享质量、文档构建及实际 diff；应用完整回归和制品结果继续覆盖未变化的源码。S6 至 S8 保持 `PENDING`，随后按顺序进入 S6。

### S6 LLM 与证据正确性

S6 为 `IN_PROGRESS`，基线为 S5 完成提交 `88804295aedd783837328dd59189604de1873e0a`；S7、S8 保持 `PENDING`。本阶段实施 M23 至 M28，只由主执行者修改，子执行者仅作本阶段只读分析。

工作项目及依赖顺序：

1. 统一出站代理与本地绕过规则；核对模型连接、容量、取消和预算边界。增加明确的协议诊断、有限传输重试、请求身份和费用未知提示，保留共享总请求预算与大输出能力。
2. 修正无词级时间的局部证据、数字和实体依据、引用传递及跨字段关系；采用本地可逆的证据引用，保留独立事实核验。
3. 仅让实际输入证据和语义策略影响快照，窗口外录制增长及执行参数变动不触发无效重算；窗口内变化、人工操作和失效租约继续拒绝过期结果。
4. 复用现有执行与提交链增加事件级有限重算、失败补做；复用整场总结 claim、三次重试和输入签名，增加分层总结及块级缓存恢复。

验收适用性：先运行模型、证据、重分析、总结、代理和 Portable 相关目标基线；通过真实 SDK 与受控 HTTP/数据库/进程边界验证重试、取消、费用未知、部分失败和恢复，不调用外部模型或上传素材。最后执行完整应用与 Portable 回归、全部 pre-commit、前端类型与生产构建、OpenAPI、文档、正式 Payload、受影响的冻结启动器、发布审计及实际 diff。依赖和来源信息按实际改动维护，版本仍在 S8 统一更新。

M23 至 M28 已进入完整本地验收。统一预算覆盖文本、结构化、传输补发与分层总结；发送前请求记录、服务回执和费用未知状态分别保存。证据采用请求内 E/Q 引用并在本地恢复原 ID，实体和数字仍需真实原文依据。媒体与快照仅覆盖实际输入范围，事件恢复会重新核对已完成候选的来源和人工状态。人工场次重新分析可复用原文补做失败整理，自动下播不授予此权限，旧请求消费也不会删除期间到达的新请求。

整场总结保留 v3 请求与结果契约，在独立版本化缓存保存成功节点；小输入仍一次分析，大输入完整分片和递归综合，全部调用共享预算。来源、策略、输入或 claim 变化时拒绝迟到写入。过长中间摘要继续完整分片压缩，可容纳的尾片沿用实际正文；未缩短的响应不会保存为成功压缩节点。最终结果写入失败后可复用已完成 root，明确重新生成仍重做最终综合。

目前修改文件目标集为 **548 passed**，最后压缩尾片修正后的总结专项为 **15 passed**。已验证真实子进程中断后复用成功节点、未确认请求保留未知费用、数据库写入失败、请求竞争、输出容量、人工保护和关闭边界。完整应用与 Portable、质量、构建、制品和远端门禁仍待完成，S6 保持 `IN_PROGRESS`。

首次完整回归为 **4289 passed、14 failed，覆盖率 85.85%**。页面与评估导出仍使用完整策略复验新语义指纹，导致 11 项读取失败；现统一在纯来源模块按冻结策略版本校验，v1/v2 保留原完整算法、v3 使用语义身份，未知版本明确拒绝。当前提交保护继续使用实时策略。另同步草稿连通测试的真实 HTTP 边界、客户端轮换替身，以及无词时间局部字幕的事实边界，保留真实时间字幕的原裁剪断言。

新增回归确认本次请求元数据会使旧评估快照补入两个 `null` 并改变人工标注摘要；离线导出和摘要现在共用局部投影，保留字段原始存在性、显式未知值及实际请求元数据。全局 API 结构没有改变。初次目标复验 122 passed、2 failed，暴露了指纹导入间接创建数据库目录的副作用；纯计算移入来源模块后，相关 69 项全部通过，包含原只读导出与两种控制台编码。完整回归和最终门禁继续执行。

修正后完整应用与 Portable 回归为 **4312 passed、覆盖率 85.87%**，包含真实 sdist/wheel 和进程恢复用例；原有 setuptools 安装接口弃用 warning 保留。全范围 Ruff 静态检查通过，后续继续共享质量门禁与正式新来源制品验收，S6 尚未完成。

业务来源固定为 `d1bf5b50f5fcbd47b14a1eabed29b898b00793b9`，五处来源信息与正式生成的 fixture 元数据同步。该最终组合的 Portable 与版本目标为 **502 passed**，完整应用与 Portable 为 **4312 passed、覆盖率 85.88%**，包含真实 wheel；全部 12 项 pre-commit 通过。前端类型、lint、格式、85 项测试和生产构建、OpenAPI 一致性、文档 22 项测试与 57 页构建均通过，完整发布审计为 **63 PASS、0 WARN、0 FAIL**。

正式 Payload 保留默认双次可重复构建，共 330 个文件；新 Lite EXE 的内嵌 Payload、LICENSE、版本、来源、清单和 1951 个冻结模块路径检查通过。制品如下，版本仍为 `0.1.20.2-alpha`，S8 统一升级：

| 制品 | 字节数 | SHA-256 | CRC32 |
| --- | ---: | --- | --- |
| `source_payload.zip` | 1560847 | `166ab6269284672825dbac838e4ec6abcded42615f0331539acd21ec88267163` | `D09F2067` |
| `BiliLiveCut-Portable-Lite-v0.1.20.2-alpha-x64.exe` | 67176454 | `8457603517954efc5807b6b88d7f9a576e7a1e9f6f50fcb87be190d5362870b7` | `83DF40C5` |

任务隔离 Portable 副本完成真实离线更新、`pip check`、Runtime 校验与四引擎 fixture 准备。实际后端、前端、console 分别异常终止后恢复唯一新服务代次，旧树和端口回收；冻结宿主异常退出清除所属进程，无关哨兵继续运行。预检后的真实端口竞争只启动一代 API，返回失败码 `1` 并清理所属进程。旧 S5 制品和日志保留，没有改动 E 盘运行目录、升级依赖或追加外部模型请求。

S6 本地及冻结制品验收已通过，保持 `IN_PROGRESS`，等待最新提交的全部适用远端 CI。S7、S8 保持 `PENDING`。

提交 `78b1151` 的首轮 CI 在 Ubuntu Python 3.13 和 Windows Python 3.11 各有一个失败，均为滴流响应截止测试；其他五个应用矩阵、Portable、审计、uv、Docker、Frontend、Documentation 和 CodeQL 均成功。Ubuntu 日志确认测试服务器的 `select.select()` 遇到高编号文件描述符后异常退出；现使用标准库 `DefaultSelector`，并增加 POSIX 真实 socket 高编号变体。Windows 本地诊断进一步确认客户端冷初始化可能耗尽 0.4 秒预算，请求尚未到达测试服务器。现在先以一次真实本地请求完成预热，再创建被测预算；保留原 0.4 秒预算、2 秒结束、实际连接关闭、单次尝试和 `deadline` 分类断言。

修正后的请求、取消、结构化与持久请求目标集为 **137 passed**；完整应用与 Portable 为 **4312 passed、覆盖率 85.87%**，包含真实 wheel。本次完整命令保留两条 warning：既有 setuptools 安装接口弃用，以及显式 `PYTHONIOENCODING=utf-8` 导致子进程输出与默认 GBK 管道解码不一致。恢复原命令环境后，相关配置持久化用例以 `-W error::pytest.PytestUnhandledThreadExceptionWarning` 复验通过，没有修改产品或屏蔽警告。本次仅修改测试与验收记录，业务来源 `d1bf5b5` 及已验证制品不变；S6 继续等待修正提交的全部适用远端检查。

随后提交 `a7e2332` 的六个应用矩阵通过，Windows Python 3.12 仍有一次滴流连接关闭断言失败，结果为 **3825 passed、1 failed**；其余工作流、Portable、审计、uv 和 coverage summary 均成功。新增真实回环 HTTP 回归在原生产代码上稳定复现两种关闭竞态：响应已进入清理时，外层原生 `Task.cancel()` 再次打断 AnyIO shield 保护的关闭；HTTPX 已设置关闭标志，后续关闭调用无法补偿。现为每个网络请求建立独立取消作用域，通过该作用域取消并等待实际清理完成，保留共享连接池与其他请求。

回归继续覆盖取消、服务关闭及真实 0.4 秒截止与清理交错的情况；在调用返回前要求清理完成、没有被再次取消、远端实际断连，保留 2 秒外层期限。首次补测另发现清理中的原始网络异常会覆盖截止分类，现保留最初中断原因，并将清理期间观察到的真实异常保存在原因链中。原滴流用例将响应头发送纳入同一断开观测范围，没有放宽期限、调用次数或关闭断言。

最终相关目标为 **140 passed**；完整应用与 Portable 为 **4315 passed、覆盖率 85.89%**，包含真实 wheel，保留一条 setuptools 安装接口弃用 warning。生产传输代码已变化，接下来固定新来源并重新验收 Payload、冻结启动器与远端 CI，S6 保持 `IN_PROGRESS`。

关闭竞态修复已固定为业务来源 `f6739917f53dd0a22deb758320e7cd09fa6cb062`。同步五处来源与正式 fixture 元数据后，Portable 与版本契约为 **502 passed**；最终组合完整回归再次通过 **4315 项测试，覆盖率 85.89%**，真实 wheel 构建和运行验证通过。全部 12 项 pre-commit、OpenAPI、文档 22 项测试与 57 页构建通过；完整发布审计为 **63 PASS / 0 WARN / 0 FAIL**。

新 Payload 双次构建一致，共 330 个文件；新 EXE 的内嵌内容、来源、LICENSE、清单和 1951 个冻结模块路径检查通过。最新制品如下，先前制品和校验记录保留：

| 制品 | 字节数 | SHA-256 | CRC32 |
| --- | ---: | --- | --- |
| `source_payload.zip` | 1561003 | `c54116d5de2c7a653ae464effc2b86f27c466fdc39bd0879ceaf0017bb738989` | `5ED86401` |
| `BiliLiveCut-Portable-Lite-v0.1.20.2-alpha-x64.exe` | 67174726 | `24b87d193d533b6ab9cc0dd29e6a218840b159373c4f424f1324d01985182729` | `BE6B4E03` |

更新后的隔离 Portable 副本再次通过离线安装、依赖与 Runtime 校验、四引擎 fixture 准备。真实后端、前端、console 异常终止均恢复唯一完整新代，旧树与端口回收；冻结宿主退出清除所属进程并保留无关哨兵。真实端口竞争仅启动一代 API，返回 `1` 并回收所属资源。本地验收已完成，S6 等待本次新提交的全部适用远端 CI，S7、S8 保持 `PENDING`。
