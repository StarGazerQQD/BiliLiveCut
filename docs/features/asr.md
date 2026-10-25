# 语音转写与模型

## ASR 流水线

安装 AI 依赖：

```powershell
pip install -e ".[asr-all]" `
  --index-url https://mirrors.aliyun.com/pypi/simple/ `
  --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple/
pip install -e ".[llm]" `
  --index-url https://mirrors.aliyun.com/pypi/simple/ `
  --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple/
# 多引擎 ASR 需要 funasr + modelscope
pip install funasr modelscope
```

```powershell
# 对已录制的片段
blc process <segment_id>
blc list-candidates       # 查看高光候选

# 边录边分析（显式覆盖全局默认值）
blc record <db_id> --pipeline
```

`RECORDING_PIPELINE_ENABLED=true` 时，Web 手动开始/恢复、预约、崩溃恢复以及未显式传参的 CLI 录制都会启用实时转写与高光分析。可在控制台“设置中心 → 全部参数 → 录制与自动化”覆盖该默认值；CLI 可用 `--pipeline` 或 `--no-pipeline` 对单次录制覆盖。`TRANSCRIPT_LLM_REFINE_ENABLED=true` 时，每个切片完成 ASR 后还会调用已配置的大模型补全标点、整理正文并生成片段概括；调用失败时保留原始 ASR，不阻断流水线。录制流水线开关从下一次开始或恢复录制生效，转写整理开关从下一次转写任务生效。

默认启用以 Fun-ASR-Nano 为首选的四层 ASR 流水线（`ASR_PRIMARY=funasr_nano`），也可切换到 Paraformer 或纯 Whisper：

```env
ASR_PRIMARY=funasr_nano       # 默认；也可设为 paraformer 或 whisper
ASR_VAD_MAX_SEGMENT_S=30      # Nano 的 FSMN-VAD 单句上限（秒）
ASR_TASK_MAX_CONCURRENCY=1    # 同时转写的录制分段数；CUDA 可按显存提高到 2～8
ASR_FALLBACK_WHISPER=true     # 主引擎失败时自动兜底
TRANSCRIPT_LLM_REFINE_ENABLED=true  # 用已配置 LLM 整理正文并生成片段概括
TRANSCRIPT_LLM_REFINE_MAX_TOKENS=65536  # 五分钟转写整理的最大输出预算
HIGHLIGHT_LLM_MAX_TOKENS=65536      # 高光复核的最大输出预算（含推理 token）
```

单个引擎失败、继续尝试回退时记录 WARNING，并保留脱敏原因；回退结果通过最终校验后记录 INFO（`fallback_completed`），注明主引擎、回退引擎和原因。可选的后台预加载失败也记录 WARNING，任务仍可按配置尝试加载或回退。热点局部窗口失败仍记录 WARNING，其他窗口及后台完整转写继续；完整转写任务最终失败仍记录 ERROR。引擎失败不代表整个片段转写失败，以最终转写结果和任务状态为准。

`ASR_TASK_MAX_CONCURRENCY` 控制任务级并行，`ASR_PRIMARY_MAX_CONCURRENCY` 等变量控制对应模型角色的同时推理数。模型池按实际模型身份复用空闲实例，同一实例的推理互斥；提高并发可能加载更多模型并增加显存占用。设备和模型选择在下一任务生效，活动任务保留原快照；并发与常驻策略在后续检查生效，空闲卸载不会中断推理。

转写整理和高光复核默认各预留 `65536` 个最大输出 token，避免推理模型在处理五分钟切片时耗尽额度而没有正文；可按模型能力分别在 `128-65536` 和 `512-65536` 范围内调低。该值是单次请求上限，实际用量仍以模型返回的 token 数为准。

实时转写页先用“录制场次”下拉选择历史直播，再显示该场全部转写；旧场次不会因为新增记录超过固定条数而消失。“重新识别”会删除当前场次可重建的自动分析结果后重新排队；若存在正在运行的任务，服务端会拒绝并说明原因。每条转写还会显示源 TS 文件名；需要直接进入 NLE 剪辑时使用“无损导出 MP4”，应用会保留原始编码并把首个有效视频帧校准到 `0s`，避免普通 TS→MP4 重封装因 AAC 预滚产生首帧黑画面。

**工作原理与成本控制**：先用零成本规则特征（音量峰值、关键词、语速突增、音频特征、弹幕热度）算出 `rule_score`；只有超过初筛阈值才调用大模型复核。新直播间默认初筛/候选/人工审核阈值为 `0.28/0.38/0.32`，自动批准/发布阈值为 `0.72/0.80`，也可通过设置中心的全局阈值调整新房间默认，`.env` 同名参数仍提供部署默认；房间级值可在“直播间”及“房间自动化与端口”中调整。Web「设置中心 → 大模型服务商」没有启用服务商时自动走**纯规则模式**，完全可用、零费用。

Event-first 热点层会先按 10 秒信号桶、90 秒直播自身滚动基线和 20 秒检测 tick 生成 `provisional HotspotEvent`。弹幕、音频、SenseVoice、可选 ASR 与缓存趋势按可用证据动态归一化，因此尚无 ASR 不会把热点分数拉成零。热点峰值会反向生成默认前 35 秒、后 55 秒的高优先级局部 ASR；完成后仍保留后台完整 ASR，用于历史、搜索、字幕与整场总结。相邻检测结果会在录制连续且语义或信号连续时跨原始分段归并，稳定结束后进入 `confirmed`；代表弹幕会同时保留高频反应和信息量较高的上下文，不再让多条 `???/666` 挤掉事件内容。EventEnricher 仅根据带 ID 的事件证据束生成结构化标题、摘要和类别；未知引用、证据外实体/数字、无归因的弹幕猜测及陈旧结果都会被拒绝并降级为保守说明。随后 ClipScorer 按完整事件的多信号、证据质量和录制连续性计算成片价值；只有达到房间阈值才关联既有候选审核链，动态边界也始终限制在同一连续录像块内，不会跨断流缺口。详细参数、生命周期、评分和证据字段见 [热点检测器说明](../reference/hotspot-detector.md)。

LLM 使用 OpenAI 兼容协议，在设置中心维护共享连接和按业务划分的有序路由；转写整理、高光分析、热点、总结和文案可以独立选择模型。具体模型和接口能力以所配置的服务为准。

## CUDA 安装与实际设备确认

`ASR_*_DEVICE=cuda` 只选择设备，不会把 CPU 版 PyTorch 变成 CUDA 版。Windows 默认依赖和 Portable Full 的随附 wheelhouse 使用 CPU 包；`torch.__version__` 带 `+cpu` 或 `torch.version.cuda is None` 表示当前 Torch 没有 CUDA 支持。NVIDIA 显卡和 `nvidia-smi` 可用也不能替代这项检查。

Portable 用户先停止服务，在程序目录执行以下命令之一。安装器只改变依赖运行时，成功后保存到 `config/torch-runtime.json` 并退出；再次启动会保留选择，不会被 CPU 基础锁覆盖。支持的配套 CUDA wheel 来自 [PyTorch 官方索引](https://download.pytorch.org/whl/torch/)，全部固定 URL、版本、Python ABI 和 SHA-256。

```powershell
# 按显卡和驱动对 PyTorch 官方发行的支持选择一种；不是本机 nvcc 的版本开关。
.\BiliLiveCut-Portable.exe --torch-runtime cu126
# 或使用 cu130；恢复默认 CPU 依赖使用 --torch-runtime cpu。
```

Lite EXE 使用其实际文件名替换示例。CUDA wheel 体积较大，此操作默认允许从官方地址获取这两个文件，Full 的其余依赖仍使用随附离线包。完全离线时，把当前 Python ABI 对应锁文件中的 Torch 与 TorchAudio wheel 放入 `vendor/wheels/`，再加 `--offline`；缺少文件或哈希不匹配会失败，不会转为联网安装。两套支持的锁为 `packaging/portable/locks/requirements-torch-{cu126|cu130}-{py311|py312}-win-x64.lock`。升级或修复虚拟环境仍读取持久化选择；现有手工安装且完全匹配这两套锁的 CUDA 配套包会被识别和保留，不配套或未知版本明确拒绝自动覆盖。

源码用户先按项目说明安装基础依赖，然后在已停止服务的目标虚拟环境中应用对应 ABI 的 CUDA 锁。例如 Python 3.12：

```powershell
uv pip install --python .venv/Scripts/python.exe --no-deps --require-hashes -r packaging/portable/locks/requirements-torch-cu126-py312-win-x64.lock
.venv/Scripts/python.exe -m pip check
.\.venv\Scripts\blc.exe doctor --yes
```

源码默认 `uv.lock` 仍是基础依赖环境；重新执行 `uv sync` 后应重新应用 CUDA 锁。使用目标环境的 `blc` 启动服务；使用 uv 时执行 `uv run --no-sync blc start`，避免启动时再次自动同步回基础环境。程序不下载模型来检测 GPU，也不在安装依赖时自动改写业务设备配置。

随后在「设置中心 → ASR」选择设备；已保存的 Web 配置优先于 `.env`，正在运行的任务仍使用开始时的快照。Fun-ASR-Nano/Paraformer 使用主设备，SenseVoice 使用辅助设备，Paraformer 的局部复核使用复核设备。Whisper 包括直接选择为主引擎时，均使用 `ASR_FALLBACK_DEVICE`。CPU 任务、媒体解码、调度及部分音频预处理仍会使用 CPU；增加并发前应确认显存余量。

FunASR 在加载前检查 Torch 构建、可见 GPU 和实际 CUDA 运算，加载后核对主模型、VAD 与标点模型参数设备；CUDA 不可用时明确失败，保留原有可选后端回退记录。Whisper 使用 CTranslate2，单独检查其 CUDA 能力，不以 Torch 的 CPU/GPU 版本代替判断；它需要对应的 CUDA/cuDNN 动态库，缺失时按诊断修复。远程转写主引擎的 device 配置不决定服务端设备。

`BiliLiveCut-Portable.exe --doctor` 显示实际解释器、Torch 构建、CUDA 可用性和 CTranslate2 可见 GPU 数；源码 `doctor` 另按当前有效角色设备检查，并在显式 CUDA 不可用时返回失败。成功安装 CUDA wheel 与本机可以执行 CUDA 推理是分别核验的两个结果。

## OpenAI 兼容远程转写

在「模型连接与业务路由」保存远程语音转写路由，然后到「设置中心 → ASR」选择该路由，并将主引擎切换为 `openai_compatible` 后保存。也可将路由留空，继续填写独立的远程转写基础地址、模型 ID 和密钥。默认继续使用本地 Fun-ASR-Nano。基础地址包含服务需要的 `/v1` 等前缀，程序追加 `/audio/transcriptions`；不接受 URL 凭据、查询参数和重定向。本机 HTTP 服务可不填密钥，公网服务使用 HTTPS。模型 ID 由服务提供方确定，程序不替换或猜测模型名称。只添加远程路由不会自动上传音频。

远程转写独立于 LLM 服务商：选择它会将标准化音频上传到所配服务。默认 `json` 接收正文，字幕使用明确的估算时间；仅在服务支持时选择 `verbose_json`，同时请求词与句的时间戳。程序校验时间戳范围、顺序、句段不重叠、响应格式、单块 2 MiB 及整次任务累计 8 MiB 响应上限，不把服务未提供的置信度当作零或满分。接口格式参考 [OpenAI 官方转写接口](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create)。

`ASR_REMOTE_CHUNK_S` 默认 300 秒、范围 1~600 秒，音频统一为 16kHz 单声道 PCM16，每块低于 20 MB，串行上传并恢复原窗口时间轴；长录音的分块边界可能截断词句，可按服务要求调整。每块上传前由父进程核对当前连接授权，撤销或变更凭据后不再许可后续上传；子进程不读取数据库。任一块失败则整个本次转写失败，不保存部分文本。空文本仍需本地 VAD 确认无语音，所以运行环境需保留现有 `asr-whisper` 音频/VAD 依赖；无需下载 Whisper 模型即可做这项核验。

远程主引擎不加载 FunASR、SenseVoice 或局部复核模型；`ASR_PRIMARY_MAX_CONCURRENCY` 控制远程任务并发，主引擎 device/keep_loaded 对 HTTP 调用不生效。`ASR_FALLBACK_WHISPER` 开启时，远程失败或文本退化会按现有机制回退本地 Whisper，需准备本地模型；关闭后保留远程失败，由任务已有的有限重试处理。HTTP 客户端本身不自动重试；`ASR_REMOTE_TIMEOUT_S` 控制读写超时，ASR 总推理预算、取消和租约丢失继续由生产子进程监管。接口响应成功并不代表费用为零，远程 ASR 费用不计入 LLM 文本预算。

## ASR 取消、无语音与整场覆盖

分段任务取消分为请求与执行收尾：已领取的转写、分析、渲染显示“取消中”，禁止重试，原租约只用于确认收尾，不能提交结果；执行、模型进程及资源释放后才显示“已取消”。无法确认模型进程或 IPC 清理时保留取消中和错误诊断，不能被过期恢复提前结算。排队任务可立即取消。异常重启后过期的取消请求会结算为已取消，不会自动重新运行；手动重试仍回到原执行阶段。正在发布的任务不接受取消，须等待发布完成或核对平台结果，避免误报未投稿。

人工批准出片也使用同一套取消收尾：任务取消和后台作业取消都能中止媒体处理，不会将用户取消重新排队；每次执行使用独占成片与封面文件名，保留既有版本。成片及文案提交前在写事务内复验原租约，取消后的迟到结果不能写回。

所有生产 Worker 与默认 CLI 转写使用受监管 ASR 子进程。模型调用、结果迭代、管道背压和标准化都受取消及有限期限控制；取消旧租约后等待原进程退出再给新尝试分配资源，提交前在写事务内复验租约。`asr_progress` / `asr_diagnostic` 记录任务、场次、租约、PID、模型阶段、音频样本数与耗时，故障保留脱敏异常链。实际现场的 GPU 卡死仍需日志与设备证据，不能由“VAD removed all audio”一行推断。

空文件、损坏 PCM 和 SDK 错误不会伪装成功；合法音频经 VAD 确认没有语音后保存 `no_speech`，继续使用已取得的音乐/笑声事件。单独安装 `.[asr-funasr]` 也会安装公共 CPU VAD 所需的 faster-whisper。

Nano 与 Paraformer 的 FSMN-VAD 固定使用 FP32 浮点参数，Nano 的语言模型仍采用自身配置的精度，避免 VAD 的 `Float / BFloat16` 冲突。在线加载通过 SDK 的 `model_revision` 使用各引擎固定版本。模型按角色、来源和配置在每个 ASR 子进程内独占复用；初始化失败释放名额，下次任务可以重试。主模型推理失败时依次回退 Paraformer、启用的 Whisper，保留主模型错误和次级错误，不将失败异常帧长期留在流水线缓存中。不同 PID、配置变化或空闲回收后的重新加载有各自独立的缓存，须结合进程及模型诊断判断。

整场总结等待活动任务收尾，终态缺口允许生成“部分完成”报告并列出缺失片段、时间和原因；后台扫描已结束场次，手动重试、补齐转写后重新生成并标明补齐完成，无需打开时间线触发。超时预算与角色并发详见[配置参考](../guide/configuration.md#asr-与-llm-有限等待)。
