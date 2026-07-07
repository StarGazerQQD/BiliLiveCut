# Changelog — 0.1.4 系列 (已归档)

> 此文件已从主 CHANGELOG.md 归档。原始版本详见 Git 历史。

## V0.1.4 Alpha (2026-07-03)

### 新增
- **GUI 账号登录**:Dashboard 新增「账号管理」Tab,点击登录弹出无痕浏览器窗口,用户扫码/密码登录后自动采集 Bilibili Cookie 并持久化存储,无需手动编辑 `.env`。
- **Cookie 统一管理**:新增 `app/core/cookie.py` 统一 Cookie 读取入口（运行时设置优先,`.env` 兜底）,所有模块（recorder/danmaku/service/cli）已统一接入。
- **Cookie 状态面板**:Dashboard 账号管理 Tab 实时展示当前登录态（UID、Cookie 摘要）,支持一键清除。

### 内部
- 新增 `app/web/login_handler.py`（Playwright 浏览器自动化登录流程）。
- 新增 `POST /api/login`、`GET /api/login/status`、`POST /api/login/clear`、`GET /api/cookie-status` 四个 API 端点。
- `launcher.exe` 重新编译。
