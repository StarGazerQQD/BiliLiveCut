# 统一配置清单与生效边界

当前注册表共 150 项：140 项 Settings 与 10 项运行时配置；评分配置含 8 个权重和 6 个标量。表中默认值均来自代码/项目默认，不导出环境凭据。

业务默认来自环境或项目配置；Web 覆盖以原键保存在 AppSetting。保存先整体校验，再在单一事务中提交；revision 防止旧表单覆盖。字段缺省保持原值，reset 删除覆盖并恢复环境/项目默认；凭据空白保持，clear 明确清空并阻止环境回退。

生效标记：new_room 为新建房间默认（已有房间不变）；next_recording 为下一场；next_task 为下一次操作或流水线阶段；next_poll 为下一次调度/保护检查；immediate 为下一次读取；restart 为下一次启动；deployment 为启动配置或已替代项，界面只读并解释原因。正在执行的任务和多窗口转写保留开始时的完整快照。

| 配置键 | 中文名称 | 分组/作用域 | 默认 | 类型/范围 | 持久化/生效 | 实际读取位置或只读原因 |
| --- | --- | --- | --- | --- | --- | --- |
| `app_env` | 运行环境 | system/global | dev | string；['dev', 'prod'] | environment/deployment | 运行环境由部署配置确定，重启后生效。 |
| `log_level` | 日志级别 | system/global | INFO | string；不限..不限 | environment/deployment | 日志输出在启动时配置；修改部署配置后重启。 |
| `admin_password` | 管理员密码 | system/global | 不回显 | string；不限..不限 | environment/deployment | 管理员凭据影响服务绑定与认证；在部署配置中修改后重启。 |
| `reviewer_accounts_json` | 审核员账号 | review/global | 不回显 | string；不限..不限 | environment/deployment | 审核员凭据在启动时加载；在部署配置中修改后重启。 |
| `review_claim_ttl_s` | 审核领取有效期 | review/global | 900 | integer；60..86400 | database/immediate | `app/web/services/review_workflow.py:148`、`app/web/services/review_workflow.py:182` |
| `review_blind_mode` | 盲审 | review/global | True | boolean；不限..不限 | database/immediate | `app/web/routers/review_router.py:336`、`app/web/routers/review_router.py:357`、`app/web/routers/review_router.py:546` |
| `storage_root` | 存储根目录 | storage/global | ./storage | string；不限..不限 | environment/deployment | 存储路径在启动时确定；变更需迁移现有数据并重启。 |
| `database_url` | 数据库连接 | storage/global | 不回显 | string；不限..不限 | environment/deployment | 数据库连接必须在数据库打开前确定；通过部署环境配置。 |
| `plugin_dir` | 插件目录 | system/global | ./storage/plugins | string；不限..不限 | environment/deployment | 插件目录由启动时的插件发现确定；修改部署配置后重启。 |
| `ffmpeg_path` | FFmpeg 程序路径 | system/global | ffmpeg | string；不限..不限 | database/immediate | `app/analysis/audio.py:158`、`app/clipping/core.py:371`、`app/clipping/core.py:959`、`app/clipping/core.py:1011`、`app/clipping/core.py:1206`、`app/clipping/cover.py:64`、`app/pipeline/collection.py:242`、`app/pipeline/collection.py:286`、`app/pipeline/collection.py:435`、`app/recording/recorder.py:482`、`app/analysis/transcription/audio_normalization.py:40`、`app/analysis/transcription/audio_normalization.py:109`、`app/analysis/transcription/backends.py:913`、`app/web/services/transcripts.py:240` |
| `ffprobe_path` | FFprobe 程序路径 | system/global | ffprobe | string；不限..不限 | database/immediate | `app/clipping/core.py:155`、`app/clipping/cover.py:132`、`app/pipeline/collection.py:340`、`app/analysis/transcription/backends.py:78`、`app/web/services/transcripts.py:288` |
| `segment_duration_s` | 录制分段目标时长 | recording/global | 300 | integer；5..600 | database/next_recording | `app/clipping/core.py:458`、`app/recording/recorder.py:497`、`app/recording/recorder.py:576`、`app/pipeline/workers/analyze.py:788`、`app/pipeline/workers/analyze.py:1136`、`app/pipeline/workers/analyze.py:1229` |
| `preferred_stream_protocol` | 首选直播流协议 | recording/global | hls | string；['hls', 'flv'] | database/next_recording | `app/commands/record.py:121`、`app/recording/recorder.py:388` |
| `stream_quality` | 直播流画质编号 | recording/global | 10000 | integer；不限..不限 | database/next_recording | `app/commands/record.py:120`、`app/recording/recorder.py:383` |
| `reconnect_max_backoff_s` | 重连最长退避 | recording/global | 30 | integer；1..不限 | database/next_recording | `app/recording/recorder.py:340`、`app/sources/bilibili/danmaku.py:309` |
| `live_poll_interval_s` | 直播状态检查间隔 | recording/global | 15 | integer；5..不限 | database/next_poll | `app/pipeline/live_monitor.py:118`、`app/recording/recorder.py:266` |
| `room_metadata_refresh_interval_s` | 直播标题刷新间隔 | recording/global | 30 | integer；5..3600 | database/next_recording | `app/recording/metadata.py:84`、`app/recording/recorder.py:219` |
| `room_metadata_refresh_timeout_s` | 标题查询超时 | recording/global | 3.0 | number；0.1..30.0 | database/next_recording | `app/recording/metadata.py:159` |
| `recording_reconnect_max_attempts` | 断流最大重试次数（0 不限制） | recording/global | 20 | integer；0..10000 | database/next_recording | `app/recording/recorder.py:357` |
| `recording_reconnect_max_elapsed_s` | 断流重试最长时间（0 不限制） | recording/global | 180 | integer；0..86400 | database/next_recording | `app/recording/recorder.py:358` |
| `live_offline_confirm_count` | 下播连续确认次数 | recording/global | 3 | integer；1..100 | database/next_poll | `app/pipeline/live_monitor.py:205` |
| `live_session_end_delay_s` | 下播收尾延迟 | recording/global | 60 | integer；0..3600 | database/next_poll | `app/pipeline/live_monitor.py:210`、`app/pipeline/live_monitor.py:238` |
| `recording_max_duration_s` | 单场录制时长上限 | recording/global | 43200 | integer；300..604800 | database/next_poll | `app/pipeline/live_monitor.py:192`、`app/pipeline/live_monitor.py:196` |
| `collect_danmaku` | 采集弹幕 | recording/global | True | boolean；不限..不限 | database/next_recording | `app/analysis/hotspot_detector.py:437`、`app/recording/recorder.py:153`、`app/pipeline/workers/analyze.py:1246` |
| `danmaku_login_retry_max_attempts` | 弹幕登录最多尝试次数 | recording/global | 5 | integer；0..100 | database/next_recording | `app/sources/bilibili/danmaku.py:236` |
| `danmaku_login_retry_interval_s` | 弹幕登录重试间隔 | recording/global | 60.0 | number；1.0..3600.0 | database/next_recording | `app/sources/bilibili/danmaku.py:239` |
| `recording_pipeline_enabled` | 录制后自动转写与分析 | recording/global | True | boolean；不限..不限 | database/next_recording | `app/core/settings_store.py:127`、`app/core/settings_store.py:133`、`app/web/services/settings.py:27`、`app/web/services/settings.py:33` |
| `require_authorization` | 要求直播间授权 | system/global | True | boolean；不限..不限 | database/immediate | `app/commands/record.py:44`、`app/commands/record.py:163`、`app/web/services/rooms.py:207`、`app/web/services/rooms.py:274`、`app/web/services/rooms.py:286`、`app/web/services/rooms.py:648`、`app/web/services/schedules.py:62` |
| `bilibili_cookie` | Bilibili Cookie | system/global | 不回显 | string；不限..不限 | database/immediate | `app/core/cookie.py:5`、`app/core/cookie.py:19`、`app/web/login_handler.py:133`、`app/web/routers/auth.py:71` |
| `whisper_model` | Whisper 模型 | asr/global | small | string；不限..不限 | database/next_task | `app/analysis/model_pool.py:32`、`app/analysis/transcription/backends.py:735`、`app/analysis/transcription/backends.py:737` |
| `whisper_device` | Whisper 默认设备 | asr/global | cpu | string；不限..不限 | database/next_task | `app/analysis/model_pool.py:34`、`app/analysis/transcription/backends.py:188`、`app/analysis/transcription/backends.py:234`、`app/analysis/transcription/backends.py:241`、`app/analysis/transcription/backends.py:258`、`app/analysis/transcription/backends.py:738` |
| `whisper_compute_type` | Whisper 计算精度 | asr/global | int8 | string；不限..不限 | database/next_task | `app/analysis/model_pool.py:33`、`app/analysis/transcription/backends.py:739` |
| `asr_resource_policy` | 模型资源不足处理 | asr/global | warn | string；['strict', 'warn'] | database/next_task | `app/analysis/transcription/backends.py:829` |
| `asr_primary` | ASR 主引擎 | asr/global | funasr_nano | string；['funasr_nano', 'funasr', 'nano', 'paraformer', 'whisper'] | database/next_task | `app/analysis/model_pool.py:36`、`app/analysis/model_pool.py:162`、`app/analysis/model_pool.py:166`、`app/analysis/model_pool.py:173`、`app/analysis/transcription/pipeline.py:140`、`app/analysis/transcription/pipeline.py:280` |
| `asr_sensevoice` | 加载 SenseVoice 辅助模型 | asr/global | True | boolean；不限..不限 | database/next_task | `app/analysis/model_pool.py:37`、`app/analysis/model_pool.py:171`、`app/analysis/transcription/backends.py:139`、`app/pipeline/workers/analyze.py:1325` |
| `asr_funasr_review` | FunASR 低质量复核 | asr/global | True | boolean；不限..不限 | database/next_task | `app/analysis/model_pool.py:39`、`app/analysis/model_pool.py:173`、`app/analysis/transcription/backends.py:140`、`app/analysis/transcription/pipeline.py:243` |
| `asr_fallback_whisper` | Whisper 兜底 | asr/global | True | boolean；不限..不限 | database/next_task | `app/analysis/model_pool.py:40`、`app/analysis/model_pool.py:175`、`app/analysis/transcription/pipeline.py:88` |
| `asr_confidence_threshold` | 旧版置信度阈值 | asr/global | -0.6 | number；不限..不限 | environment/deployment | 原始置信度不能跨引擎比较；使用 asr_review_risk_threshold。 |
| `asr_review_risk_threshold` | 复核风险阈值 | asr/global | 0.65 | number；不限..不限 | database/next_task | `app/analysis/transcription/pipeline.py:91` |
| `asr_sensevoice_enabled` | 使用 SenseVoice 辅助特征 | asr/global | True | boolean；不限..不限 | database/next_task | `app/analysis/model_pool.py:38`、`app/analysis/model_pool.py:171`、`app/pipeline/workers/analyze.py:1325` |
| `asr_vad_max_segment_s` | 语音活动切句上限 | asr/global | 30 | integer；5..120 | database/next_task | `app/analysis/model_pool.py:35`、`app/analysis/transcription/backends.py:259`、`app/analysis/transcription/backends.py:267`、`app/analysis/transcription/backends.py:281` |
| `asr_task_max_concurrency` | 同时转写任务数 | asr/global | 1 | integer；1..8 | database/next_poll | `app/core/settings_store.py:155`、`app/core/settings_store.py:164`、`app/web/services/settings.py:29`、`app/web/services/settings.py:39`、`app/analysis/model_pool.py`（按角色动态读取） |
| `asr_primary_device` | 主引擎设备 | asr/global | cpu | string；不限..不限 | database/next_task | `app/analysis/transcription/backends.py:188`、`app/analysis/transcription/backends.py:258`、`app/analysis/model_pool.py`（按角色动态读取） |
| `asr_auxiliary_device` | 辅助引擎设备 | asr/global | cpu | string；不限..不限 | database/next_task | `app/analysis/transcription/backends.py:234`、`app/analysis/transcription/backends.py:241`、`app/analysis/model_pool.py`（按角色动态读取） |
| `asr_review_device` | 复核引擎设备 | asr/global | cpu | string；不限..不限 | database/next_task | `app/analysis/transcription/backends.py:258`、`app/analysis/model_pool.py`（按角色动态读取） |
| `asr_fallback_device` | 兜底引擎设备 | asr/global | cpu | string；不限..不限 | database/next_task | `app/analysis/transcription/backends.py:738`、`app/analysis/model_pool.py`（按角色动态读取） |
| `asr_primary_max_concurrency` | 主引擎最大并发 | asr/global | 1 | integer；1..8 | database/next_poll | `app/analysis/model_pool.py`（按角色动态读取） |
| `asr_auxiliary_max_concurrency` | 辅助引擎最大并发 | asr/global | 1 | integer；1..8 | database/next_poll | `app/analysis/model_pool.py`（按角色动态读取） |
| `asr_review_max_concurrency` | 复核引擎最大并发 | asr/global | 1 | integer；1..8 | database/next_poll | `app/analysis/model_pool.py`（按角色动态读取） |
| `asr_fallback_max_concurrency` | 兜底引擎最大并发 | asr/global | 1 | integer；1..8 | database/next_poll | `app/analysis/model_pool.py`（按角色动态读取） |
| `asr_primary_keep_loaded` | 主引擎模型常驻 | asr/global | True | boolean；不限..不限 | database/next_poll | `app/analysis/model_pool.py`（按角色动态读取） |
| `asr_auxiliary_keep_loaded` | 辅助引擎模型常驻 | asr/global | False | boolean；不限..不限 | database/next_poll | `app/analysis/model_pool.py`（按角色动态读取） |
| `asr_review_keep_loaded` | 复核引擎模型常驻 | asr/global | False | boolean；不限..不限 | database/next_poll | `app/analysis/model_pool.py`（按角色动态读取） |
| `asr_fallback_keep_loaded` | 兜底引擎模型常驻 | asr/global | False | boolean；不限..不限 | database/next_poll | `app/analysis/model_pool.py`（按角色动态读取） |
| `asr_model_idle_unload_seconds` | 非驻留模型空闲卸载时间（0 关闭） | asr/global | 900 | integer；0..86400 | database/next_poll | `app/analysis/model_pool.py:109` |
| `asr_preload_on_start` | 启动预加载模型 | asr/global | False | boolean；不限..不限 | database/restart | `app/pipeline/task_worker.py:244` |
| `asr_model_revision` | 旧版全局模型版本 | asr/global | v2.0.4 | string；不限..不限 | environment/deployment | 模型版本由每个后端的模型目录统一锁定；不能使用全局版本覆盖。 |
| `llm_daily_budget` | LLM 每日预算（美元，0 不限） | llm/global | 0.0 | number；0.0..不限 | database/next_task | `app/analysis/llm.py:38` |
| `transcript_llm_refine_enabled` | LLM 整理转写与摘要 | llm/global | True | boolean；不限..不限 | database/next_task | `app/core/settings_store.py:144`、`app/core/settings_store.py:150`、`app/web/services/settings.py:28`、`app/web/services/settings.py:36` |
| `transcript_llm_refine_max_tokens` | 转写整理输出 Token 上限 | llm/global | 65536 | integer；128..65536 | database/next_task | `app/analysis/llm.py:585` |
| `highlight_llm_max_tokens` | 高光复核输出 Token 上限 | highlights/global | 65536 | integer；512..65536 | database/next_task | `app/analysis/llm.py:671`、`app/analysis/session_summary.py:303` |
| `hotspot_enrichment_llm_max_tokens` | 热点解释输出 Token 上限 | highlights/global | 65536 | integer；512..65536 | database/next_task | `app/analysis/event_enricher.py:280` |
| `trend_enabled` | 启用网感资料库 | trends/global | False | boolean；不限..不限 | database/next_task | `app/analysis/hotspot_detector.py:488`、`app/commands/maintenance.py:43`、`app/publishing/copywriter.py:80`、`app/publishing/copywriter.py:206`、`app/trends/collector.py:101`、`app/trends/scheduler.py:148`、`app/trends/scheduler.py:202`、`app/pipeline/workers/analyze.py:1331`、`app/web/services/trends.py:26`、`app/web/services/trends.py:55` |
| `trend_api_key` | 网感专用 API 密钥 | trends/global | 不回显 | string；不限..不限 | database/next_task | `app/analysis/llm.py:455` |
| `trend_base_url` | 网感专用服务地址 | trends/global |  | string；不限..不限 | database/next_task | `app/analysis/llm.py:456` |
| `trend_model` | 网感专用模型 | trends/global |  | string；不限..不限 | database/next_task | `app/analysis/llm.py:458` |
| `trend_web_search_param` | 联网搜索参数名 | trends/global | enable_search | string；不限..不限 | database/next_task | `app/analysis/llm.py:466` |
| `trend_web_search` | 启用联网搜索 | trends/global | True | boolean；不限..不限 | database/next_task | `app/trends/collector.py:111`、`app/web/services/trends.py:27` |
| `trend_max_items` | 每次网感条目上限 | trends/global | 12 | integer；1..200 | database/next_task | `app/trends/collector.py:105` |
| `trend_retention_days` | 网感资料保留天数 | trends/global | 14 | integer；1..不限 | database/next_task | `app/commands/database.py:51`、`app/trends/collector.py:146` |
| `trend_match_days` | 近期网感匹配天数 | trends/global | 7 | integer；1..不限 | database/next_task | `app/analysis/highlight.py:555`、`app/publishing/copywriter.py:85`、`app/publishing/copywriter.py:210` |
| `highlight_init_threshold` | 高光初筛阈值 | highlights/global | 0.28 | number；0.0..1.0 | database/next_task | `app/pipeline/workers/analyze.py:1364` |
| `highlight_threshold` | 高光入选阈值 | highlights/new_room_default | 0.38 | number；0.0..1.0 | database/new_room | `app/analysis/clip_scorer.py:434`、`app/analysis/threshold_learning.py:31`、`app/analysis/threshold_learning.py:237`、`app/commands/record.py:57`、`app/pipeline/workers/analyze.py:1233`、`app/web/routers/rooms.py:105`、`app/web/services/dashboard.py:72`、`app/web/services/rooms.py:671`、`app/web/services/rooms.py:702` |
| `highlight_review_threshold` | 进入人工审核阈值 | highlights/new_room_default | 0.32 | number；0.0..1.0 | database/new_room | `app/analysis/clip_scorer.py:437`、`app/commands/record.py:58`、`app/pipeline/orchestrator.py:55`、`app/pipeline/orchestrator.py:70`、`app/pipeline/workers/analyze.py:1239`、`app/web/services/rooms.py:672` |
| `highlight_auto_approve_threshold` | 高光自动通过阈值 | highlights/new_room_default | 0.72 | number；0.0..1.0 | database/new_room | `app/analysis/clip_scorer.py:441`、`app/commands/record.py:59`、`app/pipeline/orchestrator.py:54`、`app/pipeline/orchestrator.py:69`、`app/pipeline/scheduler.py:192`、`app/pipeline/scheduler.py:231`、`app/publishing/copywriter.py:242`、`app/pipeline/workers/analyze.py:1238`、`app/web/services/rooms.py:673` |
| `highlight_max_candidates_per_segment` | 每分段候选上限 | highlights/global | 4 | integer；1..12 | database/next_task | `app/pipeline/workers/analyze.py:1131`、`app/pipeline/workers/analyze.py:1137`、`app/pipeline/workers/analyze.py:1167` |
| `highlight_peak_min_distance_s` | 高光峰值最小间距 | highlights/global | 25.0 | number；5.0..300.0 | database/next_task | `app/pipeline/workers/analyze.py:1132`、`app/pipeline/workers/analyze.py:1138` |
| `highlight_min_pre_roll_s` | 成片最短前置留白 | highlights/global | 20.0 | number；0.0..180.0 | database/next_task | `app/analysis/clip_scorer.py:91`、`app/pipeline/workers/analyze.py:1437` |
| `highlight_min_post_roll_s` | 成片最短后置留白 | highlights/global | 30.0 | number；0.0..180.0 | database/next_task | `app/analysis/clip_scorer.py:92`、`app/pipeline/workers/analyze.py:1438` |
| `danmaku_event_lag_s` | 弹幕事件延迟补偿 | recording/global | 7.5 | number；0.0..60.0 | database/next_recording | `app/analysis/hotspot_detector.py:376`、`app/analysis/hotspot_detector.py:525`、`app/analysis/timeline.py:65`、`app/pipeline/workers/analyze.py:1504` |
| `hotspot_bucket_s` | 热点统计桶时长 | highlights/global | 10.0 | number；5.0..10.0 | database/next_task | `app/analysis/hotspot_detector.py:93` |
| `hotspot_baseline_window_s` | 热点滚动基线窗口 | highlights/global | 90.0 | number；60.0..120.0 | database/next_task | `app/analysis/hotspot_detector.py:94` |
| `hotspot_detector_tick_s` | 热点检测间隔 | highlights/global | 20.0 | number；15.0..30.0 | database/next_task | `app/analysis/clip_scorer.py:542`、`app/analysis/hotspot_detector.py:95`、`app/analysis/hotspot_lifecycle.py:230` |
| `hotspot_min_baseline_buckets` | 热点最少基线桶数 | highlights/global | 3 | integer；2..12 | database/next_task | `app/analysis/hotspot_detector.py:96` |
| `hotspot_detection_threshold` | 热点检测阈值 | highlights/global | 0.55 | number；0.0..1.0 | database/next_task | `app/analysis/hotspot_detector.py:97` |
| `hotspot_event_merge_gap_s` | 热点合并间隔 | highlights/global | 30.0 | number；0.0..120.0 | database/next_task | `app/analysis/hotspot_lifecycle.py:67` |
| `hotspot_event_confirm_delay_s` | 热点稳定确认延迟 | highlights/global | 60.0 | number；0.0..600.0 | database/next_task | `app/analysis/hotspot_lifecycle.py:68` |
| `hotspot_event_semantic_overlap_threshold` | 热点语义重叠阈值 | highlights/global | 0.2 | number；0.0..1.0 | database/next_task | `app/analysis/hotspot_lifecycle.py:69` |
| `hotspot_recording_gap_tolerance_s` | 录制连续性容差 | highlights/global | 1.0 | number；0.0..10.0 | database/next_task | `app/analysis/clip_scorer.py:94`、`app/analysis/hotspot_lifecycle.py:70` |
| `hotspot_asr_enabled` | 热点优先局部转写 | highlights/global | True | boolean；不限..不限 | database/next_task | `app/pipeline/workers/analyze.py:296` |
| `hotspot_asr_pre_roll_s` | 热点转写前置窗口 | highlights/global | 35.0 | number；0.0..180.0 | database/next_task | `app/pipeline/workers/analyze.py:804` |
| `hotspot_asr_post_roll_s` | 热点转写后置窗口 | highlights/global | 55.0 | number；0.0..180.0 | database/next_task | `app/pipeline/workers/analyze.py:805` |
| `hotspot_asr_priority` | 热点转写优先级 | highlights/global | 10 | integer；0..1000 | database/next_task | `app/pipeline/workers/analyze.py:823` |
| `near_live_asr_priority` | 实时转写优先级 | highlights/global | 50 | integer；0..1000 | database/next_task | `app/pipeline/scheduler.py:86`、`app/pipeline/workers/analyze.py:831`、`app/pipeline/workers/transcribe.py:589` |
| `background_asr_priority` | 历史转写优先级 | highlights/global | 100 | integer；0..1000 | database/next_task | `app/pipeline/scheduler.py:95`、`app/pipeline/workers/analyze.py:832`、`app/pipeline/workers/transcribe.py:590` |
| `auto_publish_threshold` | 自动发布分数阈值 | publishing/new_room_default | 0.8 | number；0.0..1.0 | database/new_room | `app/commands/record.py:60`、`app/pipeline/scheduler.py:65`、`app/pipeline/scheduler.py:308`、`app/web/services/dashboard.py:73`、`app/web/services/rooms.py:674`、`app/web/services/rooms.py:703` |
| `clip_loudnorm` | 响度标准化 | output/global | True | boolean；不限..不限 | database/next_task | `app/clipping/models.py:52` |
| `clip_remove_silence` | 去除首尾静音 | output/global | False | boolean；不限..不限 | database/next_task | `app/clipping/models.py:53` |
| `clip_vertical` | 竖屏输出 | output/global | False | boolean；不限..不限 | database/next_task | `app/clipping/models.py:54` |
| `clip_subtitle` | 烧录字幕 | output/global | False | boolean；不限..不限 | database/next_task | `app/clipping/models.py:55` |
| `clip_max_duration_s` | 成片时长上限 | output/global | 180 | integer；5..900 | database/next_task | `app/analysis/clip_scorer.py:93`、`app/clipping/models.py:56`、`app/web/routers/review_router.py:268` |
| `clip_video_crf` | 视频 CRF 质量（越小越清晰） | output/global | 20 | integer；0..51 | database/next_task | `app/clipping/models.py:57` |
| `clip_preset` | 视频编码速度档 | output/global | veryfast | string；['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow'] | database/next_task | `app/clipping/models.py:58` |
| `threshold_learning_min_samples` | 阈值学习最少反馈 | highlights/global | 10 | integer；3..不限 | database/next_task | `app/analysis/threshold_learning.py:130`、`app/analysis/threshold_learning.py:192`、`app/analysis/threshold_learning.py:217`、`app/analysis/threshold_learning.py:224`、`app/analysis/threshold_learning.py:225` |
| `threshold_learning_max_delta` | 阈值学习单次调整上限 | highlights/global | 0.1 | number；0.01..0.3 | database/next_task | `app/analysis/threshold_learning.py:159` |
| `schedule_check_interval_s` | 预约检查间隔 | recording/global | 30 | integer；10..300 | database/next_poll | 经统一配置快照读取 |
| `auto_recover_max_age_hours` | 启动恢复回溯小时数 | recording/global | 24 | integer；1..72 | database/next_recording | `app/web/services/rooms.py:765` |
| `min_free_disk_gb` | 高风险任务最低空闲空间 | storage/global | 10.0 | number；1.0..不限 | database/next_task | `app/pipeline/storage_lifecycle.py:100` |
| `raw_retention_days` | 原始录像保留天数 | storage/global | 7 | integer；1..90 | database/next_task | `app/pipeline/storage_lifecycle.py:184` |
| `clip_cleanup_delay_hours` | 生成成片后的原始分段清理延迟 | output/global | 24 | integer；1..720 | database/next_task | `app/pipeline/storage_lifecycle.py:188` |
| `max_analyzing` | 同时分析任务数 | system/global | 2 | integer；1..16 | database/next_poll | `app/pipeline/task_worker.py:249`、`app/pipeline/task_worker.py:342` |
| `max_rendering` | 同时渲染任务数 | system/global | 2 | integer；1..16 | database/next_poll | `app/pipeline/task_worker.py:249`、`app/pipeline/task_worker.py:343` |
| `max_publishing` | 同时投稿任务数 | system/global | 1 | integer；1..8 | database/next_poll | `app/pipeline/task_worker.py:346` |
| `worker_shutdown_timeout_seconds` | 后台任务关闭等待时间 | system/global | 30 | integer；1..3600 | database/immediate | `app/pipeline/task_worker.py:273` |
| `stale_timeout_s` | 失联任务回收时间 | system/global | 120 | integer；60..86400 | database/immediate | `app/pipeline/stale_recovery.py:54` |
| `upload_attempt_stale_s` | 未返回上传结果确认时间 | publishing/global | 600 | integer；60..86400 | database/next_task | `app/pipeline/stale_recovery.py:216`、`app/pipeline/stale_recovery.py:233` |
| `low_disk_threshold_gb` | 暂停新任务的磁盘阈值 | storage/global | 20.0 | number；1..不限 | database/next_poll | `app/pipeline/storage_lifecycle.py:121`、`app/pipeline/storage_lifecycle.py:122` |
| `critical_disk_threshold_gb` | 停止录制的紧急磁盘阈值 | storage/global | 5.0 | number；0.1..不限 | database/next_poll | `app/pipeline/storage_lifecycle.py:122`、`app/pipeline/storage_lifecycle.py:123`、`app/pipeline/storage_lifecycle.py:403` |
| `uploader` | 旧版上传器默认值 | publishing/global | manual | string；不限..不限 | environment/deployment | 实际上传方式由 biliup_enabled 控制；关闭时使用手动导出。 |
| `upload_max_retries` | 上传最大重试次数 | publishing/global | 3 | integer；0..10 | database/next_task | `app/publishing/uploader.py:411` |
| `upload_max_per_hour` | 每小时上传上限 | publishing/global | 5 | integer；1..不限 | database/next_task | `app/publishing/uploader.py:129`、`app/publishing/uploader.py:130` |
| `title_max_len` | 投稿标题字数上限 | publishing/global | 80 | integer；10..200 | database/next_task | `app/publishing/uploader.py:117`、`app/publishing/uploader.py:118` |
| `desc_max_len` | 投稿简介字数上限 | publishing/global | 2000 | integer；10..不限 | database/next_task | `app/publishing/uploader.py:121`、`app/publishing/uploader.py:122` |
| `biliup_config` | biliup 配置文件路径 | publishing/global |  | string；不限..不限 | database/next_task | `app/publishing/uploader.py:256` |
| `biliup_upload_cmd` | biliup 上传命令模板 | publishing/global | 不回显 | string；不限..不限 | database/next_task | `app/publishing/uploader.py:241`、`app/web/services/settings.py:48` |
| `notify_enabled` | 启用通知 | notifications/global | False | boolean；不限..不限 | database/next_task | `app/notify/webhook.py:45` |
| `dingtalk_webhook` | 钉钉 Webhook | notifications/global | 不回显 | string；不限..不限 | database/next_task | `app/notify/webhook.py:47`、`app/notify/webhook.py:85`、`app/notify/webhook.py:87` |
| `dingtalk_secret` | 钉钉签名密钥 | notifications/global | 不回显 | string；不限..不限 | database/next_task | `app/notify/webhook.py:88`、`app/notify/webhook.py:89` |
| `wecom_webhook` | 企业微信 Webhook | notifications/global | 不回显 | string；不限..不限 | database/next_task | `app/notify/webhook.py:48`、`app/notify/webhook.py:132`、`app/notify/webhook.py:137`、`app/notify/webhook.py:147` |
| `smtp_host` | SMTP 服务器 | notifications/global |  | string；不限..不限 | database/next_task | `app/notify/webhook.py:49`、`app/notify/webhook.py:171`、`app/notify/webhook.py:184` |
| `smtp_port` | SMTP 端口 | notifications/global | 465 | integer；1..65535 | database/next_task | `app/notify/webhook.py:185` |
| `smtp_user` | SMTP 用户名 | notifications/global |  | string；不限..不限 | database/next_task | `app/notify/webhook.py:49`、`app/notify/webhook.py:171`、`app/notify/webhook.py:176`、`app/notify/webhook.py:190` |
| `smtp_password` | SMTP 密码 | notifications/global | 不回显 | string；不限..不限 | database/next_task | `app/notify/webhook.py:190` |
| `smtp_from` | 邮件发件人 | notifications/global |  | string；不限..不限 | database/next_task | `app/notify/webhook.py:176` |
| `smtp_to` | 邮件收件人（逗号分隔） | notifications/global |  | string；不限..不限 | database/next_task | `app/notify/webhook.py:49`、`app/notify/webhook.py:171`、`app/notify/webhook.py:177` |
| `notify_on_clip` | 成片完成通知 | notifications/global | True | boolean；不限..不限 | database/next_task | `app/notify/webhook.py:227` |
| `notify_on_upload` | 投稿完成通知 | notifications/global | False | boolean；不限..不限 | database/next_task | `app/notify/webhook.py:276` |
| `notify_on_disk_alert` | 磁盘不足通知 | notifications/global | True | boolean；不限..不限 | database/next_task | `app/notify/webhook.py:243` |
| `notify_on_error` | 任务失败通知 | notifications/global | True | boolean；不限..不限 | database/next_task | `app/notify/webhook.py:261` |
| `disk_alert_threshold_gb` | 磁盘通知阈值 | notifications/global | 10 | integer；不限..不限 | database/next_poll | `app/web/routers/monitor_router.py:34`、`app/web/routers/monitor_router.py:42` |
| `biliup_enabled` | 启用 biliup 投稿 | publishing/global | False | boolean；不限..不限 | database/next_task | `app/core/settings_store.py:14`、`app/core/settings_store.py:98`、`app/web/services/settings.py:45` |
| `auto_upload` | 成片自动加入投稿队列 | publishing/global | False | boolean；不限..不限 | database/next_task | `app/core/settings_store.py:15`、`app/core/settings_store.py:106`、`app/pipeline/orchestrator.py:53`、`app/pipeline/orchestrator.py:68`、`app/pipeline/orchestrator.py:77`、`app/pipeline/scheduler.py:55`、`app/pipeline/scheduler.py:58`、`app/pipeline/scheduler.py:63`、`app/pipeline/scheduler.py:305`、`app/web/services/dashboard.py:79`、`app/web/services/rooms.py:715`、`app/web/services/settings.py:46` |
| `trend_schedule_enabled` | 启用网感定时采集 | trends/global | False | boolean；不限..不限 | database/next_task | `app/core/settings_store.py:17`、`app/trends/scheduler.py:146`、`app/trends/scheduler.py:201` |
| `trend_schedule_start` | 网感采集开始时间 | trends/global | 03:00 | string；不限..不限 | database/next_task | `app/core/settings_store.py:18`、`app/trends/scheduler.py:187`、`app/trends/scheduler.py:203` |
| `trend_schedule_end` | 网感采集结束时间 | trends/global | 05:00 | string；不限..不限 | database/next_task | `app/core/settings_store.py:19`、`app/trends/scheduler.py:188`、`app/trends/scheduler.py:204` |
| `trend_schedule_interval_min` | 网感采集间隔（分钟） | trends/global | 30 | integer；1..1440 | database/next_task | `app/core/settings_store.py:20`、`app/trends/scheduler.py:190`、`app/trends/scheduler.py:205` |
| `threshold_learning_enabled` | 全局阈值学习 | highlights/global | True | boolean；不限..不限 | database/next_task | `app/analysis/threshold_learning.py:126`、`app/analysis/threshold_learning.py:210`、`app/analysis/threshold_learning.py:222`、`app/core/settings_store.py:22` |
| `danmaku_sentiment_enabled` | 全局弹幕情绪分析 | recording/global | True | boolean；不限..不限 | database/next_recording | `app/core/settings_store.py:23`、`app/pipeline/workers/analyze.py:1243`、`app/web/services/dashboard.py:90`、`app/web/services/rooms.py:709`、`app/web/services/rooms.py:727` |
| `storage_cleanup_enabled` | 按保留策略自动清理原片（默认关闭） | storage/global | False | boolean；不限..不限 | database/next_task | `app/core/settings_store.py:24`、`app/pipeline/task_worker.py:318` |
| `scoring_configuration` | 评分权重、融合、窗口与去重 | highlights/global | 项目 scoring.yaml 与内置 audio_events 默认 | object；不限..不限 | database/next_task | `app/analysis/scoring_config.py:123` |

## 独立配置范围

- 房间配置：LiveRoom 的授权、自动录制/分析/剪辑/投稿/审核、预约、阈值、弹幕情绪，以及 room_config_json 的热词、别名、插件模式和评分参数，保留房间接口；具体房间值不被无声改成全局继承。
- LLM 服务商：llm_providers 保存完整有序列表；按 ID 保留空白密钥，clear_api_key 明确清除，重复 ID 和非有限价格拒绝保存。连接缓存使用完整凭据摘要，旧连接在活动调用结束后退役。
- Cookie：登录接口和统一配置读取相同键；清空不会重新启用环境 Cookie。
- 插件：plugin.<id>.setting.<key> 按插件 schema 整体验证和单事务保存；密码 value/default 均遮蔽；null 明确清空密码，空字符串保持。
- Launcher：web_port 位于 config/launcher.json，原子替换，下一次启动生效。源码 CLI 未传 --port 时也读取该文件，显式 --port 优先。端口与数据库属于不同保存域；旧联合接口先验证，并在数据库提交失败时补偿恢复端口，进程/系统在跨域提交间崩溃仍不具备跨文件 ACID 保证。

## 部署和算法边界

database_url 必须先于数据库打开确定；storage_root/plugin_dir 涉及路径及已有数据，不在运行中搬迁。日志级别、运行环境和管理员/审核员身份在启动时初始化，保留环境配置入口与重启说明。

BLC_APP_ROOT、BLC_SOURCE_DIR、BLC_PORTABLE、BLC_MODELS_DIR、BLC_MODEL_CONFIG_DIR、BLC_OFFLINE、BLC_JOURNAL_DIR、代理及安装器下载配置属于部署/安装诊断；不保存到被它们定位的数据库。PIP_* 仅作为安装命令的环境变量。ASR_TASK_MAX_CONCURRENCY 为规范键；MAX_TRANSCRIBING 是旧环境别名。

固定模型 ID/revision 来自后端目录；asr_model_revision 不再控制加载，asr_confidence_threshold 由统一复核风险阈值替代，uploader 由 biliup_enabled 替代，三者明确只读。关键词词库、topic_cluster 的内部聚类常量和 danmaku_sampling 的算法常量保留代码/资料文件管理，不将所有内部常量当作可随意调节的用户配置。

磁盘参数满足 critical <= min_free <= low 且告警阈值 >= min_free；告警直接使用自己的阈值。自动清理默认关闭，开启后每小时检查；手动磁盘维护可以直接执行清理。原片只按登记的单个文件删除，保留活动录制/任务/媒体操作、审核草稿、重分析、共享路径及未知文件；成片后的提前清理要求可用成片完整覆盖该分段。

biliup_config 通过上传命令模板中的 {config} 占位符使用；不猜测第三方工具的参数形式。自动入队投稿同时要求全局 auto_upload、biliup_enabled 与房间 auto_upload 开启，且候选分数达到房间 auto_publish_threshold；缺少候选或未达阈值转人工确认。

## API

GET /api/settings/configuration 返回脱敏字段清单；PATCH 同地址接受 values、reset、clear、revision。原 /api/settings 继续可用。HTTP 422 只返回字段位置、错误类别和静态提示，避免回显含密钥的输入对象。
