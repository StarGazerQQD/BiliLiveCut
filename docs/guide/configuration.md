# 配置参考

在 Web 左侧“设置中心”搜索中文名称或配置键，可以查看当前有效值、来源和生效时间。通过 `.env` 修改配置时，使用 `.env.example` 中对应的变量名。运行中的字段以设置中心显示为准。

设置首页按「采集与运行」「分析与成片」「账号与外部服务」「提醒与维护」组织入口，可以搜索功能名称并直接进入对应分类。全部参数按用途分组展示；跨分类搜索时可展开高级设置查找完整参数。

## 模型连接与业务路由

进入「设置中心 → 大模型服务」，在「服务商预设」中选择平台，再点击「添加服务」。连接管理服务名称、OpenAI 兼容 API 地址与密钥；接着选择业务能力和连接，点击「添加路由」，填写该业务的模型。已有预设的文字模型示例会填入新文字路由，远程 ASR 模型由用户填写。初次使用引导提供相同功能。

高光分析、转写整理、热点采集、整场总结、投稿文案和远程转写分别选择模型，可共用一个连接与密钥。文字业务支持多个路由按优先级回退；远程转写只使用在 ASR 设置中明确选择的路由。本地 CUDA 与模型加载设置继续独立生效。

填写自己账号的 API 密钥，并确认模型已开通。模型或推理接入点由账号决定的平台，需要从控制台复制完整 ID。点击「测试此路由」只测试该条文字路由的当前草稿；点击「保存连接与路由」才会保存整个目录。选择预设或添加连接、路由不会自动访问平台。密钥留空保留原值，清空需明确勾选；修改地址时必须重新填写密钥或清空。「自定义 OpenAI 兼容服务」可用于其他平台或自建服务。

保存采用配置版本检查，失败保留内存草稿。出现版本冲突后读取最新配置，放弃旧草稿，再在最新目录上修改，避免覆盖他人新增的连接和路由。运行中任务保留模型与业务参数，新任务使用新值。每次新的模型请求（包括文字生成重试和远程 ASR 的下一块上传）都会核对最新连接授权；停用或删除连接、停用路由、清空或轮换密钥、修改服务地址后，旧任务不能继续用旧授权发起请求，需要使用当前配置重新发起操作。已发送到服务商的请求无法撤回，其已产生的用量仍按实际响应记录。已有通用模型和热点专用配置可在页面预览转换结果；保存后使用独立路由，旧整表写入入口不再允许覆盖新目录。

测试路由会实际调用所填服务并按账号规则计费：每次测试最多两次请求，每次生成预算最多 4096 token（包括思考），同时受该路由输出上限限制。仅有推理内容时不会显示成功。预算耗尽且没有可用的参数调整时直接报告原因。远程 ASR 需要保存并选择后用短录播验证，文本连通测试不会自动上传音频。

命令行 `llm-list` 和 `llm-test` 默认操作高光分析路由；使用 `--capability analysis|transcript|trends|summary|copywriting` 指定文字业务。列表不输出密钥，测试使用与网页相同的有限调用和预算检查。远程 ASR 通过实际转写入口验证。

文字业务共用 `storage/llm_budget.json` 的每日估算账本，跨线程和进程累计时使用文件锁及原子替换。账本损坏或无法读写会报告错误并停止继续尝试模型，不将未知花费当作零；需核查服务商实际用量并恢复有效账本。预算按返回的 token 与配置价格估算，已开始的并发请求可能使当天总额超过限额，不能替代服务商账户的硬限额。

思考控制按已核实的官方端点与模型匹配：[DeepSeek](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/) 的 `deepseek-flash` / `deepseek-v4-pro` 可关闭思考；[MiniMax](https://platform.minimax.cn/docs/api-reference/text-openai-api) 的 `MiniMax-M3.1-Flash` 强制思考，使用 `reasoning_effort=low` 进行连通测试或空正文重试。自定义地址及未核实的模型保持原参数，不因返回推理内容就发送禁用思考指令。业务请求仍遵守原有输出预算，重试最多一次；推理过程不作为最终正文保存。

以下预设按官方公开接入信息核对。地址填写基础路径，不要再追加 `/chat/completions`。模型可用性、额度、价格以对应平台控制台为准；预设不自动填写价格或开通模型。

| 平台与官方接入说明 | 预填 API 地址 | 使用说明 |
| --- | --- | --- |
| [DeepSeek](https://api-docs.deepseek.com/zh-cn/) | `https://api.deepseek.com` | 使用开放平台 API Key。 |
| [阿里云百炼（Qwen）](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope) | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 北京地域通用地址；可以替换为控制台提供的业务空间专属地址。地域与 API Key 必须匹配。 |
| [火山方舟（豆包）](https://docs.volcengine.com/docs/ark/compatible-with-openai-sdk?lang=zh) | `https://ark.cn-beijing.volces.com/api/v3` | 使用已开通的 Model ID；自定义推理接入点填写自己的 `ep-…` ID。 |
| [Kimi（Moonshot）](https://platform.kimi.com/docs/get-api-key) | `https://api.moonshot.cn/v1` | 使用 API 开放平台密钥。 |
| [智谱 GLM](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction) | `https://open.bigmodel.cn/api/paas/v4` | 通用按量 API 地址。 |
| [小米 MiMo](https://mimo.mi.com/docs/zh-CN/quick-start/summary/first-api-call) | `https://api.xiaomimimo.com/v1` | 实时按量 API 地址。 |
| [硅基流动](https://docs.siliconflow.cn/docs/userguide/capabilities/text-generation) | `https://api.siliconflow.cn/v1` | 国内平台；模型填写含组织名及可能存在的 `Pro/` 前缀的完整 ID。 |
| [MiniMax](https://platform.minimax.cn/docs/api-reference/text-openai-api) | `https://api.minimax.cn/v1` | 国内平台 OpenAI 兼容地址。 |
| [百度千帆](https://cloud.baidu.com/doc/qianfan-docs/s/Fm9l6ocai) | `https://qianfan.baidubce.com/v2` | 使用千帆通用 API Key，并复制已开通的模型或推理接入点 ID。 |
| [腾讯云 TokenHub（混元等）](https://cloud.tencent.com/document/product/1823/132252) | `https://tokenhub.tencentmaas.com/v1` | 使用 TokenHub API Key；新账号优先使用此入口。 |
| [讯飞星火](https://www.xfyun.cn/doc/spark/HTTP调用文档.html) | `https://spark-api-open.xf-yun.com/v1` | 密钥填写对应版本的 **APIPassword**，不是 WebSocket 的 APIKey 或 APISecret。 |
| [魔搭 ModelScope](https://www.modelscope.cn/docs/model-service/API-Inference/intro) | `https://api-inference.modelscope.cn/v1` | 使用 ModelScope Token，复制支持 API 推理的完整 Model ID。 |
| [阶跃星辰 StepFun](https://platform.stepfun.com/) | `https://api.stepfun.com/v1` | 国内平台；从模型列表选择可用的文本模型 ID。 |
| [无问芯穹](https://docs.infini-ai.com/support/faq.html) | `https://cloud.infini-ai.com/maas/v1` | 使用 GenStudio API Key 与服务列表中的模型 ID。 |
| [腾讯混元旧平台](https://cloud.tencent.com/document/product/1729/111007) | `https://api.hunyuan.cloud.tencent.com/v1` | 供仍有旧平台服务的账号使用；新购模型已迁移至 TokenHub。 |
| [腾讯云 LKEAP 旧平台](https://cloud.tencent.com/document/product/1772/130549) | `https://api.lkeap.cloud.tencent.com/v1` | 原 DeepSeek 接口已迁移至 TokenHub，旧平台凭据不与 TokenHub 混用。 |

上述常用预设面向通用模型 API。Coding Plan、Token Plan、批量推理和国际区域可能有不同地址、凭据及适用范围，请按对应平台说明修改，不要将套餐密钥直接用于通用按量地址。

## 飞书 / Lark 运行提醒

进入「设置中心 → 提醒与通知 → 飞书 / Lark 应用机器人」，配置 App ID、App Secret、服务区域和接收对象。通知直接调用 OpenAPI，不建立 WebSocket 连接，也不需要事件订阅。

1. 在飞书开放平台创建企业自建应用，开启机器人能力。
2. 添加 `im:message:send_as_bot`（以应用身份发消息）权限，按租户要求完成审批，并发布应用版本；目标用户需在应用可用范围内。
3. 向群聊发送时，将机器人加入群聊，选择 `chat_id` 并填写对应的 `oc_…` ID。向个人发送时，选择与实际 ID 对应的 `open_id`、`union_id`、`user_id` 或 `email`。这些 ID 不能混用。
4. 国内飞书选择 `feishu`，国际版选择 `lark`；应用凭据与接收对象应属于同一服务区域。
5. 保存后点击「测试发送飞书消息」，确认目标用户或群聊收到消息，再开启「启用运行提醒」并选择提醒内容。

保存配置不会自动发送消息。测试发送由管理员显式触发，不依赖运行提醒总开关；飞书通道不需要邮件验证码。App Secret 不会在页面回显，留空保留已有密钥。访问令牌仅在内存中缓存，临近过期重新获取；配置变更后使用新凭据。

支持成片完成、投稿完成、磁盘告警、任务失败及登录态/WS 异常提醒。连接异常提醒在后台投递，同房间同原因遵循 `notify_connection_cooldown_s` 冷却间隔；飞书发送失败不会阻止录制。若测试失败，检查应用发布、权限审批、机器人群成员身份及接收 ID 类型。

环境变量对应 `LARK_APP_ID`、`LARK_APP_SECRET`、`LARK_DOMAIN`、`LARK_RECEIVE_ID_TYPE`、`LARK_RECEIVE_ID`。只发送通知无需安装飞书 SDK。

接口参考：[获取应用 tenant access token](https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal)、[发送消息与接收对象限制](https://open.feishu.cn/document/server-docs/im-v1/message/create)。

## 邮件提醒与邮箱验证

进入「设置中心 → 提醒与通知」，配置 SMTP 服务、端口、连接方式、账号/授权码、发件邮箱和一个目标邮箱。支持 SSL/TLS、STARTTLS 和明文 SMTP；明文用于可信内网中继，无认证中继可留空用户名。

保存配置不会发送邮件。点击「发送验证码」，到目标邮箱读取六位验证码并提交验证；验证码十分钟内有效，最多尝试五次，重发间隔一分钟，每小时最多发送五次。验证成功后开启「启用运行提醒」并选择提醒内容。只填写环境变量或直接开启通知，也不能绕过收件验证。

修改目标邮箱、发件人或 SMTP 配置会撤销验证，需重新验证。凭据留空保留原值；提醒开关变化不会撤销验证。运行邮件后台发送，失败信息可在该页面查看；「已交给邮件服务器」不代表邮件一定到达收件箱，请同时检查垃圾邮件。

连接提醒包含平台明确拒绝 Cookie、连续登录失败以及 WS 连续三次连接失败；普通网络异常不会被直接标记成 Cookie 过期。同房间同类连接提醒默认间隔 1800 秒，防止重试期间反复发送。通知不替代网页中的运行状态或连接日志；服务退出时尚未开始的运行邮件会被取消。

## 参数参考

下表列出配置的默认值、取值范围和生效时间，敏感字段不显示实际内容。当前配置可能受到 `.env` 或 Web 设置覆盖，因此不一定等于表中默认值。

ASR 设备值只选择推理设备，不安装 GPU 依赖；CPU 版 Torch 无法因填写 `cuda` 而使用 GPU。Portable 的 `--torch-runtime` 为独立安装操作，选择保存在 `config/torch-runtime.json`，不增加业务注册表字段。安装、离线 wheel 和实际设备诊断见[CUDA 使用说明](../features/asr.md#cuda-安装与实际设备确认)。

Web 中保存的业务设置优先于环境或项目默认值。保存前会校验整份表单；其他窗口已经修改配置时，需要先读取最新值再核对保存。“重置”恢复环境或项目默认值。凭据留空表示保留，显式清空后不再回退到环境中的旧值。

“新建房间”仅影响之后创建的房间；“下次录制”从下一场开始；“下次任务”指下一次操作或处理阶段；“下次调度检查”指下一次轮询或保护检查；“立即”指下一次读取配置；“重启后”需要重新启动服务。部署配置在界面只读，需修改 `.env` 或部署环境后重启。正在执行的任务使用开始时的配置，不随保存操作改变。

| 配置键 | 名称 | 分类 / 作用范围 | 默认值 | 类型 / 取值范围 | 生效时间 |
| --- | --- | --- | --- | --- | --- |
| `app_env` | 运行环境 | 系统 / 全局 | dev | 文本；['dev', 'prod'] | 部署配置，重启后 |
| `log_level` | 日志级别 | 系统 / 全局 | INFO | 文本 | 部署配置，重启后 |
| `admin_password` | 管理员密码 | 系统 / 全局 | 不回显 | 文本 | 部署配置，重启后 |
| `reviewer_accounts_json` | 审核员账号 | 审核 / 全局 | 不回显 | 文本 | 部署配置，重启后 |
| `review_claim_ttl_s` | 审核领取有效期 | 审核 / 全局 | 900 | 整数；60～86400 | 立即 |
| `review_blind_mode` | 盲审 | 审核 / 全局 | True | 开关 | 立即 |
| `storage_root` | 存储根目录 | 存储 / 全局 | ./storage | 文本 | 部署配置，重启后 |
| `database_url` | 数据库连接 | 存储 / 全局 | 不回显 | 文本 | 部署配置，重启后 |
| `plugin_dir` | 插件目录 | 系统 / 全局 | ./storage/plugins | 文本 | 部署配置，重启后 |
| `ffmpeg_path` | FFmpeg 程序路径 | 系统 / 全局 | ffmpeg | 文本 | 立即 |
| `ffprobe_path` | FFprobe 程序路径 | 系统 / 全局 | ffprobe | 文本 | 立即 |
| `segment_duration_s` | 录制分段目标时长 | 录制 / 全局 | 300 | 整数；5～600 | 下次录制 |
| `preferred_stream_protocol` | 同画质首选直播流协议 | 录制 / 全局 | hls | 文本；['hls', 'flv'] | 下次录制 |
| `stream_quality` | 直播流画质上限编号 | 录制 / 全局 | 10000 | 整数；1～不限 | 下次录制 |
| `reconnect_max_backoff_s` | 重连最长退避 | 录制 / 全局 | 30 | 整数；1～不限 | 下次录制 |
| `live_poll_interval_s` | 守候刷新与兼容轮询间隔 | 录制 / 全局 | 15 | 整数；5～不限 | 下次调度检查 |
| `room_metadata_refresh_interval_s` | 直播标题刷新间隔 | 录制 / 全局 | 30 | 整数；5～3600 | 下次录制 |
| `room_metadata_refresh_timeout_s` | 标题查询超时 | 录制 / 全局 | 3.0 | 数值；0.1～30.0 | 下次录制 |
| `recording_reconnect_max_attempts` | 断流最大重试次数（0 不限制） | 录制 / 全局 | 20 | 整数；0～10000 | 下次录制 |
| `recording_reconnect_max_elapsed_s` | 断流重试最长时间（0 不限制） | 录制 / 全局 | 180 | 整数；0～86400 | 下次录制 |
| `live_offline_confirm_count` | 下播连续确认次数（查询型来源） | 录制 / 全局 | 3 | 整数；1～100 | 下次调度检查 |
| `live_session_end_delay_s` | 下播收尾延迟 | 录制 / 全局 | 60 | 整数；0～3600 | 下次调度检查 |
| `recording_max_duration_s` | 单场录制时长上限 | 录制 / 全局 | 43200 | 整数；300～604800 | 下次调度检查 |
| `collect_danmaku` | 采集弹幕 | 录制 / 全局 | True | 开关 | 下次录制 |
| `danmaku_login_retry_max_attempts` | 弹幕登录最多尝试次数 | 录制 / 全局 | 5 | 整数；0～100 | 下次录制 |
| `danmaku_login_retry_interval_s` | 弹幕登录重试间隔 | 录制 / 全局 | 60.0 | 数值；1.0～3600.0 | 下次录制 |
| `recording_pipeline_enabled` | 录制后自动转写与分析 | 录制 / 全局 | True | 开关 | 下次录制 |
| `require_authorization` | 要求直播间授权 | 系统 / 全局 | True | 开关 | 立即 |
| `bilibili_cookie` | Bilibili Cookie | 系统 / 全局 | 不回显 | 文本 | 立即 |
| `whisper_model` | Whisper 模型 | 语音转写 / 全局 | small | 文本 | 下次任务 |
| `whisper_device` | Whisper 默认设备 | 语音转写 / 全局 | cpu | 文本 | 下次任务 |
| `whisper_compute_type` | Whisper 计算精度 | 语音转写 / 全局 | int8 | 文本 | 下次任务 |
| `asr_resource_policy` | 模型资源不足处理 | 语音转写 / 全局 | warn | 文本；['strict', 'warn'] | 下次任务 |
| `asr_primary` | ASR 主引擎 | 语音转写 / 全局 | funasr_nano | 文本；['funasr_nano', 'paraformer', 'whisper', 'openai_compatible'] | 下次任务 |
| `asr_remote_route_id` | 远程转写路由 | 语音转写 / 全局 | 空 | 已保存且启用的 ASR 路由 ID；空为独立 API 配置 | 下次任务 |
| `asr_remote_base_url` | 远程转写基础地址 | 语音转写 / 全局 | 空 | HTTP(S)，无查询参数、凭据和片段 | 下次任务 |
| `asr_remote_api_key` | 远程转写密钥 | 语音转写 / 全局 | 空 | 敏感字段，不回显 | 下次任务 |
| `asr_remote_model` | 远程模型 ID | 语音转写 / 全局 | 空 | 未选择路由且启用远程时必填 | 下次任务 |
| `asr_remote_language` | 远程语言 | 语音转写 / 全局 | 空 | ISO 639 语言码；空为自动 | 下次任务 |
| `asr_remote_response_format` | 远程响应格式 | 语音转写 / 全局 | json | json/verbose_json | 下次任务 |
| `asr_remote_timeout_s` | 远程请求超时 | 语音转写 / 全局 | 120.0 | 数值；1～600 | 下次任务 |
| `asr_remote_chunk_s` | 远程分块秒数 | 语音转写 / 全局 | 300 | 整数；1～600 | 下次任务 |
| `asr_sensevoice` | 加载 SenseVoice 辅助模型 | 语音转写 / 全局 | True | 开关 | 下次任务 |
| `asr_funasr_review` | FunASR 低质量复核 | 语音转写 / 全局 | True | 开关 | 下次任务 |
| `asr_fallback_whisper` | Whisper 兜底 | 语音转写 / 全局 | True | 开关 | 下次任务 |
| `asr_review_risk_threshold` | 复核风险阈值 | 语音转写 / 全局 | 0.65 | 数值 | 下次任务 |
| `asr_sensevoice_enabled` | 使用 SenseVoice 辅助特征 | 语音转写 / 全局 | True | 开关 | 下次任务 |
| `asr_vad_max_segment_s` | 语音活动切句上限 | 语音转写 / 全局 | 30 | 整数；5～120 | 下次任务 |
| `asr_task_max_concurrency` | 同时转写任务数 | 语音转写 / 全局 | 1 | 整数；1～8 | 下次调度检查 |
| `asr_primary_device` | 主引擎设备 | 语音转写 / 全局 | cpu | 文本 | 下次任务 |
| `asr_auxiliary_device` | 辅助引擎设备 | 语音转写 / 全局 | cpu | 文本 | 下次任务 |
| `asr_review_device` | 复核引擎设备 | 语音转写 / 全局 | cpu | 文本 | 下次任务 |
| `asr_fallback_device` | 兜底引擎设备 | 语音转写 / 全局 | cpu | 文本 | 下次任务 |
| `asr_primary_max_concurrency` | 主引擎最大并发 | 语音转写 / 全局 | 1 | 整数；1～8 | 下次调度检查 |
| `asr_auxiliary_max_concurrency` | 辅助引擎最大并发 | 语音转写 / 全局 | 1 | 整数；1～8 | 下次调度检查 |
| `asr_review_max_concurrency` | 复核引擎最大并发 | 语音转写 / 全局 | 1 | 整数；1～8 | 下次调度检查 |
| `asr_fallback_max_concurrency` | 兜底引擎最大并发 | 语音转写 / 全局 | 1 | 整数；1～8 | 下次调度检查 |
| `asr_primary_keep_loaded` | 主引擎模型常驻 | 语音转写 / 全局 | True | 开关 | 下次调度检查 |
| `asr_auxiliary_keep_loaded` | 辅助引擎模型常驻 | 语音转写 / 全局 | False | 开关 | 下次调度检查 |
| `asr_review_keep_loaded` | 复核引擎模型常驻 | 语音转写 / 全局 | False | 开关 | 下次调度检查 |
| `asr_fallback_keep_loaded` | 兜底引擎模型常驻 | 语音转写 / 全局 | False | 开关 | 下次调度检查 |
| `asr_model_idle_unload_seconds` | 非驻留模型空闲卸载时间（0 关闭） | 语音转写 / 全局 | 900 | 整数；0～86400 | 下次调度检查 |
| `asr_preload_on_start` | 启动预加载模型 | 语音转写 / 全局 | False | 开关 | 重启后 |
| `asr_inference_base_timeout_s` | ASR 加载及推理基础超时（秒） | 语音转写 / 全局 | 600 | 整数；30～7200 | 下次任务 |
| `asr_inference_timeout_rtf` | 每秒音频追加预算（秒） | 语音转写 / 全局 | 10 | 数值；1～100 | 下次任务 |
| `asr_inference_max_timeout_s` | 单次 ASR 总预算上限（秒） | 语音转写 / 全局 | 7200 | 整数；60～43200 | 下次任务 |
| `llm_request_timeout_s` | LLM 单次请求超时（秒） | 大模型 / 全局 | 90 | 数值；1～600 | 下次任务 |
| `llm_daily_budget` | LLM 每日预算（美元，0 不限） | 大模型 / 全局 | 0.0 | 数值；0.0～不限 | 下次任务 |
| `transcript_llm_refine_enabled` | LLM 整理转写与摘要 | 大模型 / 全局 | True | 开关 | 下次任务 |
| `transcript_llm_refine_max_tokens` | 转写整理输出 Token 上限 | 大模型 / 全局 | 65536 | 整数；128～65536 | 下次任务 |
| `highlight_llm_max_tokens` | 高光复核输出 Token 上限 | 高光分析 / 全局 | 65536 | 整数；512～65536 | 下次任务 |
| `hotspot_enrichment_llm_max_tokens` | 热点解释输出 Token 上限 | 高光分析 / 全局 | 65536 | 整数；512～65536 | 下次任务 |
| `trend_enabled` | 启用网感资料库 | 热点趋势 / 全局 | False | 开关 | 下次任务 |
| `trend_api_key` | 网感专用 API 密钥 | 热点趋势 / 全局 | 不回显 | 文本 | 下次任务 |
| `trend_base_url` | 网感专用服务地址 | 热点趋势 / 全局 |  | 文本 | 下次任务 |
| `trend_model` | 网感专用模型 | 热点趋势 / 全局 |  | 文本 | 下次任务 |
| `trend_web_search_param` | 联网搜索参数名 | 热点趋势 / 全局 | enable_search | 文本 | 下次任务 |
| `trend_web_search` | 启用联网搜索 | 热点趋势 / 全局 | True | 开关 | 下次任务 |
| `trend_max_items` | 每次网感条目上限 | 热点趋势 / 全局 | 12 | 整数；1～200 | 下次任务 |
| `trend_retention_days` | 网感资料保留天数 | 热点趋势 / 全局 | 14 | 整数；1～不限 | 下次任务 |
| `trend_match_days` | 近期网感匹配天数 | 热点趋势 / 全局 | 7 | 整数；1～不限 | 下次任务 |
| `highlight_init_threshold` | 高光初筛阈值 | 高光分析 / 全局 | 0.28 | 数值；0.0～1.0 | 下次任务 |
| `highlight_threshold` | 高光入选阈值 | 高光分析 / 新建房间默认 | 0.38 | 数值；0.0～1.0 | 新建房间 |
| `highlight_review_threshold` | 进入人工审核阈值 | 高光分析 / 新建房间默认 | 0.32 | 数值；0.0～1.0 | 新建房间 |
| `highlight_auto_approve_threshold` | 高光自动通过阈值 | 高光分析 / 新建房间默认 | 0.72 | 数值；0.0～1.0 | 新建房间 |
| `highlight_max_candidates_per_segment` | 每分段候选上限 | 高光分析 / 全局 | 4 | 整数；1～12 | 下次任务 |
| `highlight_peak_min_distance_s` | 高光峰值最小间距 | 高光分析 / 全局 | 25.0 | 数值；5.0～300.0 | 下次任务 |
| `highlight_min_pre_roll_s` | 成片最短前置留白 | 高光分析 / 全局 | 20.0 | 数值；0.0～180.0 | 下次任务 |
| `highlight_min_post_roll_s` | 成片最短后置留白 | 高光分析 / 全局 | 30.0 | 数值；0.0～180.0 | 下次任务 |
| `danmaku_event_lag_s` | 弹幕事件延迟补偿 | 录制 / 全局 | 7.5 | 数值；0.0～60.0 | 下次录制 |
| `hotspot_bucket_s` | 热点统计桶时长 | 高光分析 / 全局 | 10.0 | 数值；5.0～10.0 | 下次任务 |
| `hotspot_baseline_window_s` | 热点滚动基线窗口 | 高光分析 / 全局 | 90.0 | 数值；60.0～120.0 | 下次任务 |
| `hotspot_detector_tick_s` | 热点检测间隔 | 高光分析 / 全局 | 20.0 | 数值；15.0～30.0 | 下次任务 |
| `hotspot_min_baseline_buckets` | 热点最少基线桶数 | 高光分析 / 全局 | 3 | 整数；2～12 | 下次任务 |
| `hotspot_detection_threshold` | 热点检测阈值 | 高光分析 / 全局 | 0.55 | 数值；0.0～1.0 | 下次任务 |
| `hotspot_event_merge_gap_s` | 热点合并间隔 | 高光分析 / 全局 | 30.0 | 数值；0.0～120.0 | 下次任务 |
| `hotspot_event_confirm_delay_s` | 热点稳定确认延迟 | 高光分析 / 全局 | 60.0 | 数值；0.0～600.0 | 下次任务 |
| `hotspot_event_semantic_overlap_threshold` | 热点语义重叠阈值 | 高光分析 / 全局 | 0.2 | 数值；0.0～1.0 | 下次任务 |
| `hotspot_recording_gap_tolerance_s` | 录制连续性容差 | 高光分析 / 全局 | 1.0 | 数值；0.0～10.0 | 下次任务 |
| `hotspot_asr_enabled` | 热点优先局部转写 | 高光分析 / 全局 | True | 开关 | 下次任务 |
| `hotspot_asr_pre_roll_s` | 热点转写前置窗口 | 高光分析 / 全局 | 35.0 | 数值；0.0～180.0 | 下次任务 |
| `hotspot_asr_post_roll_s` | 热点转写后置窗口 | 高光分析 / 全局 | 55.0 | 数值；0.0～180.0 | 下次任务 |
| `hotspot_asr_priority` | 热点转写优先级 | 高光分析 / 全局 | 10 | 整数；0～1000 | 下次任务 |
| `near_live_asr_priority` | 实时转写优先级 | 高光分析 / 全局 | 50 | 整数；0～1000 | 下次任务 |
| `background_asr_priority` | 历史转写优先级 | 高光分析 / 全局 | 100 | 整数；0～1000 | 下次任务 |
| `auto_publish_threshold` | 自动发布分数阈值 | 上传发布 / 新建房间默认 | 0.8 | 数值；0.0～1.0 | 新建房间 |
| `clip_loudnorm` | 响度标准化 | 剪辑输出 / 全局 | True | 开关 | 下次任务 |
| `clip_remove_silence` | 去除首尾静音 | 剪辑输出 / 全局 | False | 开关 | 下次任务 |
| `clip_vertical` | 竖屏输出 | 剪辑输出 / 全局 | False | 开关 | 下次任务 |
| `clip_subtitle` | 烧录字幕 | 剪辑输出 / 全局 | False | 开关 | 下次任务 |
| `clip_danmaku` | 烧录弹幕 | 剪辑输出 / 全局 | False | 开关 | 下次任务 |
| `clip_danmaku_mode` | 弹幕显示模式 | 剪辑输出 / 全局 | scroll | scroll/top/bottom | 下次任务 |
| `clip_danmaku_font_size` | 弹幕字号（1080 高画面） | 剪辑输出 / 全局 | 36 | 整数；12～120 | 下次任务 |
| `clip_danmaku_color` | 弹幕颜色 | 剪辑输出 / 全局 | FFFFFF | 六位 RGB 十六进制 | 下次任务 |
| `clip_danmaku_opacity` | 弹幕不透明度 | 剪辑输出 / 全局 | 0.85 | 数值；0～1 | 下次任务 |
| `clip_danmaku_duration_s` | 弹幕显示秒数 | 剪辑输出 / 全局 | 6.0 | 数值；2～20 | 下次任务 |
| `clip_danmaku_max_lines` | 弹幕最大行数 | 剪辑输出 / 全局 | 8 | 整数；1～20 | 下次任务 |
| `clip_max_duration_s` | 成片时长上限 | 剪辑输出 / 全局 | 180 | 整数；5～900 | 下次任务 |
| `clip_video_crf` | 视频 CRF 质量（越小越清晰） | 剪辑输出 / 全局 | 20 | 整数；0～51 | 下次任务 |
| `clip_preset` | 视频编码速度档 | 剪辑输出 / 全局 | veryfast | 文本；['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow'] | 下次任务 |
| `clip_video_encoder` | 视频编码设备（CPU 关闭硬件加速） | 剪辑输出 / 全局 | cpu | cpu/nvidia/intel/amd | 下次任务 |
| `clip_hardware_quality` | 硬件编码质量值 | 剪辑输出 / 全局 | 23 | 整数；1～51 | 下次任务 |
| `clip_hardware_preset` | 硬件编码速度档 | 剪辑输出 / 全局 | balanced | speed/balanced/quality | 下次任务 |
| `threshold_learning_min_samples` | 阈值学习最少反馈 | 高光分析 / 全局 | 10 | 整数；3～不限 | 下次任务 |
| `threshold_learning_max_delta` | 阈值学习单次调整上限 | 高光分析 / 全局 | 0.1 | 数值；0.01～0.3 | 下次任务 |
| `schedule_check_interval_s` | 预约检查间隔 | 录制 / 全局 | 30 | 整数；10～300 | 下次调度检查 |
| `auto_recover_max_age_hours` | 启动恢复回溯小时数 | 录制 / 全局 | 24 | 整数；1～72 | 下次录制 |
| `min_free_disk_gb` | 高风险任务最低空闲空间 | 存储 / 全局 | 10.0 | 数值；1.0～不限 | 下次任务 |
| `raw_retention_days` | 原始录像保留天数 | 存储 / 全局 | 7 | 整数；1～90 | 下次任务 |
| `clip_cleanup_delay_hours` | 生成成片后的原始分段清理延迟 | 剪辑输出 / 全局 | 24 | 整数；1～720 | 下次任务 |
| `max_analyzing` | 同时分析任务数 | 系统 / 全局 | 2 | 整数；1～16 | 下次调度检查 |
| `max_rendering` | 同时渲染任务数 | 系统 / 全局 | 2 | 整数；1～16 | 下次调度检查 |
| `max_publishing` | 同时投稿任务数 | 系统 / 全局 | 1 | 整数；1～8 | 下次调度检查 |
| `worker_shutdown_timeout_seconds` | 后台任务关闭等待时间 | 系统 / 全局 | 30 | 整数；1～3600 | 立即 |
| `stale_timeout_s` | 失联任务回收时间 | 系统 / 全局 | 120 | 整数；60～86400 | 立即 |
| `upload_attempt_stale_s` | 未返回上传结果确认时间 | 上传发布 / 全局 | 600 | 整数；60～86400 | 下次任务 |
| `low_disk_threshold_gb` | 暂停新任务的磁盘阈值 | 存储 / 全局 | 20.0 | 数值；1～不限 | 下次调度检查 |
| `critical_disk_threshold_gb` | 停止录制的紧急磁盘阈值 | 存储 / 全局 | 5.0 | 数值；0.1～不限 | 下次调度检查 |
| `upload_max_retries` | 上传最大重试次数 | 上传发布 / 全局 | 3 | 整数；0～10 | 下次任务 |
| `upload_max_per_hour` | 每小时上传上限 | 上传发布 / 全局 | 5 | 整数；1～不限 | 下次任务 |
| `title_max_len` | 投稿标题字数上限 | 上传发布 / 全局 | 80 | 整数；10～200 | 下次任务 |
| `desc_max_len` | 投稿简介字数上限 | 上传发布 / 全局 | 2000 | 整数；10～不限 | 下次任务 |
| `biliup_config` | biliup 配置文件路径 | 上传发布 / 全局 |  | 文本 | 下次任务 |
| `biliup_upload_cmd` | biliup 上传命令模板 | 上传发布 / 全局 | 不回显 | 文本 | 下次任务 |
| `notify_enabled` | 启用通知 | 通知 / 全局 | False | 开关 | 下次任务 |
| `dingtalk_webhook` | 钉钉 Webhook | 通知 / 全局 | 不回显 | 文本 | 下次任务 |
| `dingtalk_secret` | 钉钉签名密钥 | 通知 / 全局 | 不回显 | 文本 | 下次任务 |
| `wecom_webhook` | 企业微信 Webhook | 通知 / 全局 | 不回显 | 文本 | 下次任务 |
| `lark_app_id` | 飞书应用 App ID | 通知 / 全局 |  | 文本 | 下次发送 |
| `lark_app_secret` | 飞书应用 App Secret | 通知 / 全局 | 不回显 | 文本 | 下次发送 |
| `lark_domain` | 飞书服务区域 | 通知 / 全局 | feishu | feishu / lark | 下次发送 |
| `lark_receive_id` | 飞书接收对象 ID | 通知 / 全局 |  | 文本 | 下次发送 |
| `lark_receive_id_type` | 飞书接收对象类型 | 通知 / 全局 | chat_id | chat_id / open_id / union_id / user_id / email | 下次发送 |
| `smtp_host` | SMTP 服务器 | 通知 / 全局 |  | 文本 | 下次任务 |
| `smtp_port` | SMTP 端口 | 通知 / 全局 | 465 | 整数；1～65535 | 下次任务 |
| `smtp_user` | SMTP 用户名 | 通知 / 全局 |  | 文本 | 下次任务 |
| `smtp_password` | SMTP 密码 | 通知 / 全局 | 不回显 | 文本 | 下次任务 |
| `smtp_from` | 邮件发件人 | 通知 / 全局 |  | 文本 | 下次任务 |
| `smtp_to` | 提醒目标邮箱（需验证） | 通知 / 全局 |  | 单个邮箱 | 验证后发送 |
| `smtp_security` | SMTP 连接方式 | 通知 / 全局 | ssl | ssl / starttls / plain | 下次发送 |
| `notify_on_connection` | 登录态与 WS 连接异常提醒 | 通知 / 全局 | True | 开关 | 下次提醒 |
| `notify_connection_cooldown_s` | 同类连接提醒间隔 | 通知 / 全局 | 1800 | 秒；60～86400 | 下次提醒 |
| `notify_on_clip` | 成片完成通知 | 通知 / 全局 | True | 开关 | 下次任务 |
| `notify_on_upload` | 投稿完成通知 | 通知 / 全局 | False | 开关 | 下次任务 |
| `notify_on_disk_alert` | 磁盘不足通知 | 通知 / 全局 | True | 开关 | 下次任务 |
| `notify_on_error` | 任务失败通知 | 通知 / 全局 | True | 开关 | 下次任务 |
| `disk_alert_threshold_gb` | 磁盘通知阈值 | 通知 / 全局 | 10 | 整数 | 下次调度检查 |
| `biliup_enabled` | 启用 biliup 投稿 | 上传发布 / 全局 | False | 开关 | 下次任务 |
| `auto_upload` | 成片自动加入投稿队列 | 上传发布 / 全局 | False | 开关 | 下次任务 |
| `trend_schedule_enabled` | 启用网感定时采集 | 热点趋势 / 全局 | False | 开关 | 下次任务 |
| `trend_schedule_start` | 网感采集开始时间 | 热点趋势 / 全局 | 03:00 | 文本 | 下次任务 |
| `trend_schedule_end` | 网感采集结束时间 | 热点趋势 / 全局 | 05:00 | 文本 | 下次任务 |
| `trend_schedule_interval_min` | 网感采集间隔（分钟） | 热点趋势 / 全局 | 30 | 整数；1～1440 | 下次任务 |
| `threshold_learning_enabled` | 全局阈值学习 | 高光分析 / 全局 | True | 开关 | 下次任务 |
| `danmaku_sentiment_enabled` | 全局弹幕情绪分析 | 录制 / 全局 | True | 开关 | 下次录制 |
| `storage_cleanup_enabled` | 按保留策略自动清理原片（默认关闭） | 存储 / 全局 | False | 开关 | 下次任务 |
| `scoring_configuration` | 评分权重、融合、窗口与去重 | 高光分析 / 全局 | 项目 scoring.yaml 与内置 audio_events 默认 | 对象 | 下次任务 |

## 独立配置范围

- 房间配置：LiveRoom 的授权、自动录制/分析/剪辑/投稿/审核、预约、阈值、弹幕情绪，以及 room_config_json 的热词、别名、插件模式和评分参数，保留房间接口；具体房间值不被无声改成全局继承。
- 模型目录：`model_configuration` 保存服务连接与业务路由；尚未转换时兼容 `llm_providers`。按连接 ID 保留空白密钥，`clear_api_key` 明确清除，重复 ID、孤立引用和非有限价格拒绝保存。连接缓存按连接 ID 与地址索引，通过常量时间比较 SDK 已持有的完整凭据值判断能否复用；凭据及其摘要不进入缓存键，旧连接在活动调用结束后退役。
- Cookie：登录接口和统一配置读取相同键；清空不会重新启用环境 Cookie。
- 插件：`plugin.<id>.setting.<key>` 按插件 schema 整体验证和单事务保存；密码 value/default 均遮蔽；null 明确清空密码，空字符串保持。
- Launcher：web_port 位于 config/launcher.json，原子替换，下一次启动生效。源码 CLI 未传 --port 时也读取该文件，显式 --port 优先。端口与数据库属于不同保存域；旧联合接口先验证，并在数据库提交失败时补偿恢复端口，进程/系统在跨域提交间崩溃仍不具备跨文件 ACID 保证。

## 部署和算法边界

database_url 必须先于数据库打开确定；storage_root/plugin_dir 涉及路径及已有数据，不在运行中搬迁。日志级别、运行环境和管理员/审核员身份在启动时初始化，保留环境配置入口与重启说明。

BLC_APP_ROOT、BLC_SOURCE_DIR、BLC_PORTABLE、BLC_MODELS_DIR、BLC_MODEL_CONFIG_DIR、BLC_OFFLINE、BLC_JOURNAL_DIR、代理及安装器下载配置属于部署/安装诊断；不保存到被它们定位的数据库。PIP_* 仅作为安装命令的环境变量。ASR 任务并发只接受 ASR_TASK_MAX_CONCURRENCY，不再接受旧环境变量别名。

固定模型 ID/revision 来自后端目录；复核阈值使用 asr_review_risk_threshold，上传方式使用 Web 开关 biliup_enabled。历史配置项已删除，不会迁移。业务配置统一经 `/api/settings/configuration` 保存；启动端口使用独立 `/api/settings/port`，旧联合写入接口已移除。关键词词库、topic_cluster 的内部聚类常量和 danmaku_sampling 的算法常量保留代码/资料文件管理，不将所有内部常量当作可随意调节的用户配置。

磁盘参数满足 critical <= min_free <= low 且告警阈值 >= min_free；告警直接使用自己的阈值。自动清理默认关闭，开启后每小时检查；手动磁盘维护可以直接执行清理。原片只按登记的单个文件删除，保留活动录制/任务/媒体操作、审核草稿、重分析、共享路径及未知文件；成片后的提前清理要求可用成片完整覆盖该分段。

biliup_config 通过上传命令模板中的 {config} 占位符使用；不猜测第三方工具的参数形式。自动入队投稿同时要求全局 auto_upload、biliup_enabled 与房间 auto_upload 开启，且候选分数达到房间 auto_publish_threshold；缺少候选或未达阈值转人工确认。

## API

`GET /api/settings/configuration` 返回脱敏字段清单；`PATCH` 同地址接受 `values`、`reset`、`clear`、`revision`。`GET /api/settings` 读取启动状态；`PATCH /api/settings/port` 保存下次启动使用的 `web_port`。参数错误返回 HTTP 422，包含字段位置、错误类别和提示，不回显密钥等敏感输入。

## 录制重试保护

`recording_reconnect_max_attempts` 与 `recording_reconnect_max_elapsed_s` 分别限制连续失败次数和重试时长；默认 20 次 / 180 秒。单项为 0 时关闭该项，两项同时为 0 仍保留 20 次安全上限。只有实际媒体达到目标分段时长才视为恢复，短末片不会重置预算。永久录制错误暂停自动重新启动，需处理后手动恢复。诊断与最小日志采集见 [Portable 排错](../../packaging/portable/USER_GUIDE_ZH.md#反复显示录制异常)。

## ASR 与 LLM 有限等待

ASR 预算为 `min(asr_inference_max_timeout_s, asr_inference_base_timeout_s + 实测音频秒数 × asr_inference_timeout_rtf)`，覆盖资源等待、模型加载、调用、结果迭代与 IPC；音频标准化另有 300 秒上限且可取消。父进程一次预约可能用到的全部角色，避免跨进程资源死锁，因此兜底角色并发为 1 时也会限制整条管线并发。提高并发前需考虑每个模型槽独立占用的内存/显存。

LLM 每次 SDK 请求受 `llm_request_timeout_s` 限制，不再执行 SDK 隐式重试；业务空响应重试和服务商故障切换仍有明确次数。停机时同步 HTTP 可能在期限内继续收尾，任务/总结代数使其迟到结果不可写回。
