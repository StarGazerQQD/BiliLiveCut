# Changelog — 0.1.5 系列 (已归档)

> 此文件已从主 CHANGELOG.md 归档。原始版本详见 Git 历史。

## V0.1.5.1 Alpha (2026-07-03)

### 修复
- **设置开关自动取消勾选**:`sw-biliup` / `sw-auto` 两个上传开关在用户点击后、保存完成前的 5 秒轮询间隔内会被 `loadUploads()` 覆盖回旧值,导致"刚勾上又自动取消"。新增 `switchesDirty` 脏标记,用户操作后到保存完成前阻止轮询覆盖。

## V0.1.5 Alpha (2026-07-03)

### 重构
- **去 Anthropic 化**:全网感资料库与 LLM 模块移除 "Anthropic/Claude" 硬编码文字,统一使用"大模型""LLM"等通用表述。
- **趋势采集独立 API 接入**:新增 `TREND_API_KEY` / `TREND_BASE_URL` / `TREND_MODEL` 配置项,语料采集可使用独立模型(如 DeepSeek V4),不再依赖通用 LLM 多模型列表。

### 变更
- `app/core/config.py`:新增 `trend_api_key`、`trend_base_url` 字段;废弃 `anthropic_model` 回退链。
- `app/analysis/llm.py`:新增 `call_trend_search()` 专用函数,趋势采集独立 API 优先,通用 LLM 兜底。
- `app/trends/collector.py`:改用 `call_trend_search()`。
- `.env.example`:移除 `ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL`,新增 `TREND_API_KEY`/`TREND_BASE_URL`。
- Dashboard HTML/JS、CLI 帮助文本、README 等 8+ 处 Anthropic 文案已统一修正。
- 版本号更新至 `V0.1.5 Alpha`。
