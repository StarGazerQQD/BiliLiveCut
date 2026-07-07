# Changelog — 0.1.8 系列 (已归档)

> 此文件已从主 CHANGELOG.md 归档。原始版本详见 Git 历史。

## V0.1.8.1 Alpha (2026-07-04)

### P2 运营增强
- **P2.1 Dashboard 统计分析**: `GET /api/analytics` + 核心指标/分数分布/每日趋势 Canvas 图表/直播间 TOP10 排行
- **P2.2 多通道通知**: 钉钉/企业微信机器人 Webhook + SMTP 邮件;切片完成/磁盘不足/任务失败实时推送
- 配置: `.env` 新增 `NOTIFY_*` / `DINGTALK_*` / `WECOM_*` / `SMTP_*` 通知配置项

---

## V0.1.8 Alpha (2026-07-04)

### P0 管线强化
- **P0.1 Whisper hotword 注入**: `room_config.hotwords` -> Whisper `initial_prompt` 参数
- **P0.2 aliases 纠错**: `room_config.aliases` -> 转写文本自动替换专有名词
- **P0.3 Dashboard 批量操作**: `POST /api/candidates/batch` + 全选/批量批准/批量拒绝 UI
- **P0.4 ASS 字幕模板**: CRUD + 导入 .ass 提取样式 + 导出完整 ASS 文件

---
