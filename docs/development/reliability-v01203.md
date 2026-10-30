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
| S4 录制断流恢复 | IN_PROGRESS | M13 至 M15 | 短暂断流、连续短片、无进度、正常下播和人工暂停按状态处理 |
| S5 崩溃与持久断点恢复 | PENDING | M16 至 M22 | 关键提交点终止后可恢复，不丢失已确认结果，不重复外部副作用 |
| S6 LLM 与证据正确性 | PENDING | M23 至 M28 | 保留大容量和真实性校验，各类故障分别可诊断、可恢复 |
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
