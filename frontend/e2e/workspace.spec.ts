import { test, expect, type Page } from "@playwright/test";
async function mock(page: Page) {
  await page.route("**/*", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (!/^\/(api|auth|review\/api|collection\/api)/.test(path)) {
      await route.continue();
      return;
    }
    let data: unknown = [];
    if (path === "/auth/status") data = { enabled: false };
    if (path === "/api/dashboard")
      data = {
        rooms: [],
        live_sources: [],
        counts: { candidates: 0, clips: 0, active_sessions: 0 },
      };
    if (path === "/api/recording-imports")
      data = {
        imports: [],
        max_video_bytes: 53687091200,
        max_comment_bytes: 33554432,
      };
    if (path === "/api/jobs") data = { jobs: [] };
    if (path === "/api/topics") data = { topics: [] };
    if (path === "/api/plugins") data = { plugins: [] };
    if (path === "/api/model-configuration")
      data = {
        revision: 0,
        source: "routes",
        connections: [],
        routes: [],
        active_counts: {},
        asr_selection: "",
      };
    if (path === "/api/cookie-status") data = { has_cookie: false };
    if (path === "/api/login/qrcode") data = { status: "idle", task_id: null };
    if (path === "/review/api/queue")
      data = {
        items: [],
        counts: { pending: 0, claimed: 0, reviewed: 0 },
        total: 0,
        has_more: false,
      };
    if (path === "/api/settings/configuration")
      data = {
        revision: "1",
        fields: [
          {
            key: "segment_duration_s",
            label: "分段时长",
            description: "常用设置",
            group: "recording",
            value: 60,
            type: "integer",
            editable: true,
            default: 60,
            effect: "next_recording",
          },
          {
            key: "advanced_one",
            label: "高级设置示例",
            group: "recording",
            value: "test",
            type: "string",
            editable: true,
            default: "test",
          },
        ],
      };
    await route.fulfill({ json: data });
  });
}
test.beforeEach(async ({ page }) => mock(page));

test("logs default to newest first and keep the selected order after filtering and refresh", async ({
  page,
}) => {
  let refreshed = false;
  await page.route("**/api/logs?**", async (route) => {
    const logs = [
      {
        id: 1,
        created_at: "2026-10-22T01:00:00",
        level: "WARNING",
        message: "采集：较早日志",
      },
      {
        id: 3,
        created_at: "2026-10-22T02:00:00Z",
        level: "ERROR",
        message: "采集：同秒较新日志",
      },
      {
        id: 2,
        created_at: "2026-10-22T10:00:00+08:00",
        level: "ERROR",
        message: "采集：同秒较早日志",
      },
    ];
    if (refreshed)
      logs.unshift({
        id: 4,
        created_at: "2026-10-22T03:00:00Z",
        level: "ERROR",
        message: "采集：刷新新增日志",
      });
    const level = new URL(route.request().url()).searchParams.get("level");
    return route.fulfill({
      json: level ? logs.filter((entry) => entry.level === level) : logs,
    });
  });
  await page.goto("/logs");
  const messages = page.locator("p.font-mono");
  await expect(page.getByLabel("日志排序")).toHaveValue("desc");
  await expect(messages).toHaveText([
    "采集：同秒较新日志",
    "采集：同秒较早日志",
    "采集：较早日志",
  ]);
  await page.getByLabel("日志排序").selectOption("asc");
  await expect(messages.first()).toHaveText("采集：较早日志");
  await page.getByLabel("日志等级").selectOption("ERROR");
  await page.getByLabel("搜索日志内容").fill("同秒");
  await expect(messages).toHaveText([
    "采集：同秒较早日志",
    "采集：同秒较新日志",
  ]);
  await page.getByLabel("日志排序").selectOption("desc");
  await page.getByLabel("搜索日志内容").clear();
  refreshed = true;
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect(messages.first()).toHaveText("采集：刷新新增日志");
  await expect(page.getByLabel("日志排序")).toHaveValue("desc");
});

test.describe("log dates in the browser timezone", () => {
  test.use({ timezoneId: "Asia/Shanghai" });

  test("date selection queries the whole local day and clearing keeps other filters", async ({
    page,
  }) => {
    const requested: URL[] = [];
    await page.route("**/api/logs?**", async (route) => {
      const url = new URL(route.request().url());
      requested.push(url);
      return route.fulfill({
        json: url.searchParams.has("start_at")
          ? [
              {
                id: 1,
                created_at: "2026-10-21T16:00:00Z",
                level: "ERROR",
                message: "所选日期的历史日志",
              },
            ]
          : [
              {
                id: 2,
                created_at: "2026-10-23T16:00:00Z",
                level: "ERROR",
                message: "最新日期日志",
              },
            ],
      });
    });
    await page.goto("/logs");
    await expect(page.getByText("最新日期日志", { exact: true })).toBeVisible();
    await page.getByLabel("日志等级").selectOption("ERROR");
    await page.getByLabel("日志排序").selectOption("asc");
    await page.getByLabel("日志日期").fill("2026-10-22");
    await expect(page.getByText("所选日期的历史日志")).toBeVisible();
    expect(requested.at(-1)?.searchParams.get("start_at")).toBe(
      "2026-10-21T16:00:00.000Z",
    );
    expect(requested.at(-1)?.searchParams.get("end_at")).toBe(
      "2026-10-22T16:00:00.000Z",
    );
    await page.getByRole("button", { name: "刷新", exact: true }).click();
    await expect(page.getByLabel("日志日期")).toHaveValue("2026-10-22");
    await page.getByRole("button", { name: "清除日期", exact: true }).click();
    await expect(page.getByText("最新日期日志", { exact: true })).toBeVisible();
    await expect(page.getByLabel("日志日期")).toHaveValue("");
    await expect(page.getByLabel("日志等级")).toHaveValue("ERROR");
    await expect(page.getByLabel("日志排序")).toHaveValue("asc");
    await page.getByRole("button", { name: "刷新", exact: true }).click();
    await expect
      .poll(() => requested.at(-1)?.searchParams.has("start_at"))
      .toBe(false);
    expect(requested.at(-1)?.searchParams.has("start_at")).toBe(false);
    expect(requested.at(-1)?.searchParams.has("end_at")).toBe(false);
  });
});

test("settings groups are searchable and open the selected parameter category", async ({
  page,
}) => {
  await page.goto("/settings");
  for (const name of [
    "采集与运行",
    "分析与成片",
    "账号与外部服务",
    "提醒与维护",
  ]) {
    await expect(
      page.getByRole("heading", { name, exact: true }),
    ).toBeVisible();
  }
  await page.getByLabel("搜索设置功能").fill("邮箱");
  await expect(page.getByRole("link", { name: /提醒与通知/ })).toBeVisible();
  await expect(page.getByRole("link", { name: /直播录制与弹幕/ })).toHaveCount(
    0,
  );
  await page.getByLabel("搜索设置功能").fill("");
  await page.getByRole("link", { name: /直播录制与弹幕/ }).click();
  await expect(page).toHaveURL(/group=recording/);
  await expect(page.getByLabel("分段时长", { exact: false })).toBeVisible();
});

test("email must be saved and verified before notifications are enabled", async ({
  page,
}) => {
  let sent = 0;
  let verified = false;
  let saved = false;
  await page.route("**/api/settings/configuration", async (route) => {
    if (route.request().method() === "PATCH") {
      saved = true;
      verified = false;
      await route.fulfill({ json: { revision: 2 } });
      return;
    }
    await route.fulfill({
      json: {
        revision: saved ? 2 : 1,
        fields: [
          {
            key: "smtp_to",
            value: saved ? "changed@example.invalid" : "owner@example.invalid",
          },
          { key: "smtp_host", value: "smtp.example.invalid" },
          { key: "smtp_port", value: 465 },
          { key: "smtp_security", value: "ssl" },
          { key: "smtp_user", value: "sender@example.invalid" },
          { key: "notify_enabled", value: false },
        ],
      },
    });
  });
  await page.route("**/api/settings/notifications/email**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/verification-code")) {
      sent++;
    }
    if (path.endsWith("/verify")) {
      expect(route.request().postDataJSON()).toEqual({
        challenge_id: "test-challenge-id",
        code: "123456",
      });
      verified = true;
    }
    await route.fulfill({
      json: {
        recipient: saved ? "changed@example.invalid" : "owner@example.invalid",
        verified,
        can_notify: false,
        challenge_id: sent && !verified ? "test-challenge-id" : null,
        attempts_remaining: 5,
      },
    });
  });
  await page.goto("/settings/notifications");
  await expect(page.getByText("邮箱未验证", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "验证邮箱", exact: true }),
  ).toBeDisabled();
  await page
    .getByLabel("目标邮箱", { exact: true })
    .fill("changed@example.invalid");
  await expect(
    page.getByRole("button", { name: "发送验证码", exact: true }),
  ).toBeDisabled();
  expect(sent).toBe(0);
  await page.getByRole("button", { name: "保存设置", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "发送验证码", exact: true }),
  ).toBeEnabled();
  expect(sent).toBe(0);
  await page.getByRole("button", { name: "发送验证码", exact: true }).click();
  await page.getByLabel("邮箱验证码", { exact: true }).fill("123456");
  await page.getByRole("button", { name: "验证邮箱", exact: true }).click();
  await expect(page.getByText("邮箱已验证", { exact: true })).toBeVisible();
  await expect(
    page.getByText("开启运行提醒后生效", { exact: true }),
  ).toBeVisible();
});

test("room cards show event connection state and monitoring errors", async ({
  page,
}) => {
  await page.route("**/api/dashboard", (route) =>
    route.fulfill({
      json: {
        rooms: [
          {
            id: 100,
            uploader_name: "事件守候的主播",
            auto_record: true,
            monitor: {
              detection_mode: "events",
              connection_state: "failed",
              error: "开播事件连接失败，正在重连；不会将断连视为下播",
            },
          },
        ],
        counts: {},
      },
    }),
  );
  await page.goto("/rooms");
  await expect(page.getByText("开播事件 · 正在重连")).toBeVisible();
  await expect(
    page.getByText("开播事件连接失败，正在重连；不会将断连视为下播"),
  ).toBeVisible();
});

test("room sampling rules retain defaults, preserve errors and only update the selected room", async ({
  page,
}) => {
  const defaults = {
    danmaku_rate: 0.3,
    gift_rate: 0.8,
    superchat_rate: 1,
    interact_rate: 1,
    high_density_enabled: true,
    high_density_threshold: 1000,
    high_density_rate: 0.1,
  };
  let saved = { ...defaults };
  let attempts = 0;
  await page.route("**/api/dashboard", (route) =>
    route.fulfill({
      json: {
        rooms: [
          {
            id: 3,
            platform: "bilibili",
            uploader_name: "正在录制的主播",
            running: true,
            room_config: { hotwords: ["词典"], danmaku_sampling: saved },
          },
          { id: 4, platform: "bilibili", uploader_name: "另一个主播" },
        ],
        counts: {},
      },
    }),
  );
  await page.route("**/api/rooms/3", async (route) => {
    attempts++;
    const body = route.request().postDataJSON();
    expect(body).toEqual({
      room_config: {
        danmaku_sampling: {
          ...defaults,
          danmaku_rate: 0.29,
          gift_rate: 0,
          high_density_enabled: false,
        },
      },
    });
    if (attempts === 1) {
      await route.fulfill({ status: 400, json: { detail: "保存暂时失败" } });
      return;
    }
    saved = body.room_config.danmaku_sampling;
    await route.fulfill({ json: { status: "updated" } });
  });
  await page.goto("/rooms");
  const buttons = page.getByRole("button", { name: "弹幕保留", exact: true });
  await buttons.nth(0).click();
  const dialog = page.getByRole("dialog");
  const ordinary = dialog
    .getByLabel("普通弹幕保留率（%）", { exact: false })
    .first();
  const gift = dialog.getByLabel("礼物保留率（%）", { exact: false });
  await expect(ordinary).toHaveValue("30");
  await expect(gift).toHaveValue("80");
  await expect(
    dialog.getByLabel("SC 保留率（%）", { exact: false }),
  ).toHaveValue("100");
  await expect(
    dialog.getByLabel("进场等互动保留率（%）", { exact: false }),
  ).toHaveValue("100");
  await expect(dialog.getByLabel("高密度阈值", { exact: false })).toHaveValue(
    "1000",
  );
  await ordinary.fill("29");
  await gift.fill("0");
  await dialog.getByRole("switch", { name: "启用高密度规则" }).click();
  await expect(
    dialog.getByLabel("高密度阈值", { exact: false }),
  ).toBeDisabled();
  await dialog.getByRole("button", { name: "保存弹幕规则" }).click();
  await expect(dialog.getByRole("alert")).toContainText("保存暂时失败");
  await expect(ordinary).toHaveValue("29");
  await expect(gift).toHaveValue("0");
  await dialog.getByRole("button", { name: "保存弹幕规则" }).click();
  await expect(dialog).toHaveCount(0);
  expect(attempts).toBe(2);
  await buttons.nth(0).click();
  await expect(ordinary).toHaveValue("29");
  await dialog.getByRole("button", { name: "恢复默认值" }).click();
  await expect(ordinary).toHaveValue("30");
  expect(attempts).toBe(2);
  await dialog.getByRole("button", { name: "放弃修改" }).click();
  await expect(ordinary).toHaveValue("29");
  await page.keyboard.press("Escape");
  await buttons.nth(1).click();
  await expect(ordinary).toHaveValue("30");
  await expect(gift).toHaveValue("80");
  await expect(
    dialog.getByRole("switch", { name: "启用高密度规则" }),
  ).toBeChecked();
});

for (const path of ["/rooms", "/"]) {
  test(`room deletion requires confirmation and refreshes the list on ${path}`, async ({
    page,
  }) => {
    let removed = false;
    let deletions = 0;
    await page.route("**/api/dashboard", (route) =>
      route.fulfill({
        json: {
          rooms: removed
            ? []
            : [{ id: 88, room_id: 123, uploader_name: "待删除的主播" }],
          counts: {},
        },
      }),
    );
    await page.route("**/api/rooms/88", (route) => {
      expect(route.request().method()).toBe("DELETE");
      deletions++;
      removed = true;
      return route.fulfill({ json: { status: "deleted" } });
    });
    await page.goto(path);
    await page.getByRole("button", { name: "删除", exact: true }).click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toContainText("已有录制、弹幕和切片仍会保留");
    await page.getByRole("button", { name: "取消", exact: true }).click();
    await expect(dialog).toHaveCount(0);
    expect(deletions).toBe(0);
    await expect(
      page.getByRole("heading", { name: "待删除的主播" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "删除", exact: true }).click();
    await page.getByRole("button", { name: "确认删除", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: "待删除的主播" }),
    ).toHaveCount(0);
    await expect(dialog).toHaveCount(0);
    expect(deletions).toBe(1);
  });
}

test("room deletion shows recording conflicts and permits a manual retry", async ({
  page,
}) => {
  let attempts = 0;
  await page.route("**/api/dashboard", (route) =>
    route.fulfill({
      json: {
        rooms: [{ id: 88, room_id: 123, uploader_name: "状态变化的主播" }],
        counts: {},
      },
    }),
  );
  await page.route("**/api/rooms/88", (route) => {
    attempts++;
    return attempts === 1
      ? route.fulfill({
          status: 409,
          json: { detail: "直播间正在录制或收尾，请先停止录制再删除。" },
        })
      : route.fulfill({ json: { status: "deleted" } });
  });
  await page.goto("/rooms");
  await page.getByRole("button", { name: "删除", exact: true }).click();
  await page.getByRole("button", { name: "确认删除", exact: true }).click();
  await expect(page.getByRole("alertdialog").getByRole("alert")).toContainText(
    "请先停止录制再删除",
  );
  expect(attempts).toBe(1);
  await page.getByRole("button", { name: "确认删除", exact: true }).click();
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  expect(attempts).toBe(2);
});

test("recording rooms explain why deletion is unavailable", async ({
  page,
}) => {
  await page.route("**/api/dashboard", (route) =>
    route.fulfill({
      json: {
        rooms: [{ id: 88, running: true, uploader_name: "正在录制的主播" }],
        counts: {},
      },
    }),
  );
  await page.goto("/rooms");
  await expect(
    page.getByRole("button", { name: "删除", exact: true }),
  ).toBeDisabled();
  await expect(page.getByText("停止录制后可删除")).toBeVisible();
});

test("workspace renders and opens every navigation page without errors", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "工作台概览", exact: true }),
  ).toBeVisible();
  for (const [path, title] of [
    ["/rooms", "直播间"],
    ["/imports", "录播分析"],
    ["/recording", "录制状态"],
    ["/schedules", "录制预约"],
    ["/transcripts", "语音转写"],
    ["/danmaku", "弹幕热度"],
    ["/sessions", "场次时间线"],
    ["/review", "人工审片"],
    ["/topics", "主题与合集"],
    ["/clips", "成品切片"],
    ["/uploads", "上传与发布"],
    ["/trends", "网感资料库"],
    ["/tasks", "任务队列"],
    ["/analytics", "数据分析"],
    ["/monitor", "运行状态"],
    ["/logs", "错误日志"],
    ["/settings", "设置中心"],
    ["/settings/llm", "模型连接与业务路由"],
    ["/settings/login", "Bilibili 账号"],
    ["/settings/plugins", "插件中心"],
    ["/settings/subtitles", "字幕模板"],
    ["/settings/intros", "片头与片尾"],
  ]) {
    await page.goto(path);
    await expect(
      page.getByRole("heading", { name: title, exact: true }),
    ).toBeVisible();
    await expect(page.getByText("页面暂时无法显示")).toHaveCount(0);
  }
  expect(errors).toEqual([]);
});
test("empty imports settle and configuration counts follow advanced visibility", async ({
  page,
}) => {
  await page.goto("/imports");
  await expect(page.getByText("正在读取导入记录")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: /还没有导入/ })).toBeVisible();
  await page.goto("/settings/configuration");
  await expect(page.getByText("显示 1 / 1 项常用配置")).toBeVisible();
  await page.getByRole("switch", { name: "显示高级设置" }).click();
  await expect(page.getByText("显示 2 / 2 项配置")).toBeVisible();
});
test("editing and switching routes keeps the draft without a browser leave warning", async ({
  page,
}) => {
  let dialogs = 0;
  page.on("dialog", async (dialog) => {
    dialogs++;
    await dialog.dismiss();
  });
  await page.goto("/settings/configuration");
  const field = page.getByLabel("分段时长", { exact: false });
  await field.fill("120");
  if (test.info().project.name === "mobile")
    await page.getByRole("button", { name: "展开导航" }).click();
  await page
    .getByRole("link", { name: "录播分析", exact: true })
    .first()
    .click();
  await expect(
    page.getByRole("heading", { name: "录播分析", exact: true }),
  ).toBeVisible();
  if (test.info().project.name === "mobile")
    await page.getByRole("button", { name: "展开导航" }).click();
  await page
    .getByRole("link", { name: "设置中心", exact: true })
    .first()
    .click();
  await page.getByRole("link", { name: /全部参数/ }).click();
  await expect(field).toHaveValue("120");
  expect(dialogs).toBe(0);
});
test("empty optional ASR settings do not block saving other configuration", async ({
  page,
}) => {
  await page.route("**/api/settings/configuration", async (route) => {
    if (route.request().method() === "PATCH") {
      expect(route.request().postDataJSON()).toEqual({
        revision: "optional-settings",
        values: { segment_duration_s: 120 },
        clear: [],
        reset: [],
      });
      await route.fulfill({ json: {} });
      return;
    }
    await route.fulfill({
      json: {
        revision: "optional-settings",
        fields: [
          {
            key: "segment_duration_s",
            label: "分段时长",
            group: "recording",
            type: "integer",
            value: 60,
          },
          {
            key: "asr_remote_base_url",
            label: "远程转写地址",
            group: "asr",
            type: "string",
            value: "",
          },
        ],
      },
    });
  });
  await page.goto("/settings/configuration");
  await page.getByLabel("分段时长", { exact: false }).fill("120");
  const saved = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/settings/configuration" &&
      response.request().method() === "PATCH",
  );
  await page.getByRole("button", { name: "保存修改", exact: true }).click();
  expect((await saved).status()).toBe(200);
});
test("401 pauses reads and retains settings under the reauthentication dialog", async ({
  page,
}) => {
  // 本用例验证手动刷新；通知通道拒绝访问时使用 HTTP，通知恢复另有协议回归。
  await page.route("**/api/events", (route) =>
    route.fulfill({ status: 403, json: { detail: "通知通道不可用" } }),
  );
  await page.goto("/settings/configuration");
  const field = page.getByLabel("分段时长", { exact: false });
  await field.fill("90");
  let expiredReads = 0;
  await page.route("**/api/settings/configuration", (route) => {
    expiredReads++;
    return route.fulfill({ status: 401, json: { detail: "expired" } });
  });
  await page.getByRole("button", { name: "读取最新配置" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(
    page.getByText("登录已失效", { exact: false }).first(),
  ).toBeVisible();
  await expect(field).toHaveValue("90");
  await page.clock.install();
  await page.clock.fastForward(30000);
  expect(expiredReads).toBe(1);
  await expect(field).toHaveValue("90");
});

test.describe("session timezone", () => {
  test.use({ timezoneId: "America/Los_Angeles" });

  test("timeline list and detail show GMT+8 independently of browser timezone", async ({
    page,
  }) => {
    const session = {
      session_id: 1,
      session_title: "时区测试场次",
      started_at: "2026-10-20T14:39:08+00:00",
      duration_s: 774,
      status: "stopped",
    };
    await page.route("**/api/sessions/timeline?**", (route) =>
      route.fulfill({ json: [session] }),
    );
    await page.route("**/api/sessions/1/timeline?**", (route) =>
      route.fulfill({
        json: { session, timezone: "GMT+8", counts: {}, points: [] },
      }),
    );
    await page.goto("/sessions");
    const expected = "2026/10/20 22:39:08 GMT+8 · 时长 12:54";
    await expect(page.getByText(expected, { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "查看场次时间线" }).click();
    await expect(page).toHaveURL(/session=1/);
    await expect(page.getByText(expected, { exact: true })).toBeVisible();
  });
});

test("historical session cards send the real session_id", async ({ page }) => {
  await page.route("**/api/sessions/history?**", (route) =>
    route.fulfill({
      json: [
        {
          session_id: 41,
          status: "stopped",
          started_at: "2026-10-16T00:00:00Z",
          transcript_count: 1,
          danmaku_count: 0,
        },
        {
          session_id: 7,
          status: "stopped",
          started_at: "2026-10-15T00:00:00Z",
          transcript_count: 1,
          danmaku_count: 0,
        },
      ],
    }),
  );
  await page.goto("/transcripts");
  const request = page.waitForRequest(
    (request) =>
      new URL(request.url()).pathname === "/api/transcripts" &&
      new URL(request.url()).searchParams.get("session_id") === "41",
  );
  await page.getByRole("button", { name: "查看本场转写" }).first().click();
  await request;
  await expect(page).toHaveURL(/session=41/);
});

for (const kind of ["transcripts", "clips"] as const) {
  test(`${kind} library groups sessions and paginates using real ids`, async ({
    page,
  }) => {
    let itemCalls = 0;
    await page.route("**/api/sessions/history?**", (route) => {
      const params = new URL(route.request().url()).searchParams;
      const sessions: object[] = Array.from({ length: 14 }, (_, index) => ({
        session_id: index + 100,
        session_title: `测试场次 ${index + 100}`,
        started_at: "2026-10-16T00:00:00Z",
        matching_count: 45,
      }));
      sessions.push({
        session_id: null,
        session_title: "未关联场次",
        matching_count: 1,
      });
      const offset = Number(params.get("offset"));
      return route.fulfill({
        json: sessions.slice(offset, offset + Number(params.get("limit"))),
      });
    });
    await page.route(`**/api/${kind}?**`, (route) => {
      itemCalls += 1;
      const params = new URL(route.request().url()).searchParams;
      const entries = Array.from({ length: 45 }, (_, index) => ({
        id: 701 + index,
        segment_id: 901 + index,
        segment_seq: index,
        session_id: Number(params.get("session_id")),
        display_number: index + 1,
        title: `成品标题 ${index + 1}`,
        text: `转写正文 ${index + 1}`,
        status: "ready",
        source_mp4_available: true,
      }));
      const offset = Number(params.get("offset"));
      return route.fulfill({
        json: (params.get("q") ? entries.slice(-1) : entries).slice(
          offset,
          offset + Number(params.get("limit")),
        ),
      });
    });
    await page.goto(`/${kind}`);
    await expect(
      page.getByRole("button", {
        name: kind === "clips" ? "查看本场成品" : "查看本场转写",
      }),
    ).toHaveCount(12);
    expect(itemCalls).toBe(0);
    await page.getByRole("button", { name: "下一页", exact: true }).click();
    await expect(page.getByText("测试场次 112", { exact: true })).toBeVisible();
    await page
      .getByRole("button", {
        name: kind === "clips" ? "查看本场成品" : "查看本场转写",
      })
      .first()
      .click();
    await expect(page).toHaveURL(/session=112/);
    await expect(page).not.toHaveURL(/offset=/);
    await expect(
      page.getByText(
        kind === "clips" ? /本场成品 #1$/ : /场次 #112 · 分段 #1$/,
      ),
    ).toBeVisible();
    if (kind === "clips") {
      await expect(page.locator("video").first()).toHaveAttribute(
        "src",
        "/api/clips/701/video",
      );
    }
    await page.getByRole("button", { name: "下一页", exact: true }).click();
    await expect(page).toHaveURL(/offset=30/);
    await expect(
      page.getByText(
        kind === "clips" ? /本场成品 #31$/ : /场次 #112 · 分段 #31$/,
      ),
    ).toBeVisible();
    if (kind === "clips") {
      const statusRequest = page.waitForRequest((request) => {
        const url = new URL(request.url());
        return (
          url.pathname === "/api/clips" &&
          url.searchParams.get("status") === "ready" &&
          url.searchParams.get("offset") === "0"
        );
      });
      await page.getByLabel("成品状态").selectOption("ready");
      await statusRequest;
      await expect(page).not.toHaveURL(/offset=/);
      await page.getByRole("button", { name: "下一页", exact: true }).click();
      await expect(page).toHaveURL(/offset=30/);
    }
    await page
      .getByPlaceholder(
        kind === "clips" ? "搜索成品标题或简介…" : "搜索转写内容…",
      )
      .fill("目标");
    await expect(page).not.toHaveURL(/offset=/);
    await expect(
      page.getByText(
        kind === "clips" ? /本场成品 #45$/ : /场次 #112 · 分段 #45$/,
      ),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "下一页", exact: true }),
    ).toBeDisabled();
    const target =
      kind === "clips"
        ? "/api/clips/745/publish"
        : "/api/transcripts/745/retranscribe";
    await page.route(`**${target}`, (route) =>
      route.fulfill({ json: { status: "accepted" } }),
    );
    const mutation = page.waitForRequest(
      (request) =>
        new URL(request.url()).pathname === target &&
        request.method() === "POST",
    );
    await page
      .getByRole("button", {
        name: kind === "clips" ? "发布 / 标记就绪" : "重新转写",
        exact: true,
      })
      .click();
    await page
      .getByRole("alertdialog")
      .getByRole("button", { name: "确认操作", exact: true })
      .click();
    await mutation;
    await page.getByRole("button", { name: "返回场次", exact: false }).click();
    await expect(page).not.toHaveURL(/session=|offset=/);
    await page.getByRole("button", { name: "下一页", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: "未关联场次", exact: true }),
    ).toBeVisible();
    const unknownRequest = page.waitForRequest((request) => {
      const url = new URL(request.url());
      return (
        url.pathname === `/api/${kind}` &&
        url.searchParams.get("unknown_session") === "true" &&
        !url.searchParams.has("session_id")
      );
    });
    await page
      .getByRole("button", {
        name: kind === "clips" ? "查看本场成品" : "查看本场转写",
      })
      .last()
      .click();
    await unknownRequest;
    await expect(page).toHaveURL(/unknown_session=1/);
    await expect(page).not.toHaveURL(/offset=/);
  });
}

test("queue selection uses candidate ids and reports partial failure", async ({
  page,
}) => {
  await page.route("**/review/api/queue?**", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            id: 17,
            session_id: 41,
            status: "pending",
            review_status: "pending",
            reason: "值得审核的高光",
            score: 0.8,
            start_ts: "2026-10-16T00:00:00Z",
            end_ts: "2026-10-16T00:00:30Z",
            claim: { active: false },
          },
        ],
        counts: { pending: 1, claimed: 0, reviewed: 0 },
        total: 1,
        has_more: false,
      },
    }),
  );
  await page.route("**/api/candidates/batch", async (route) => {
    expect(route.request().postDataJSON()).toEqual({
      action: "approve",
      candidate_ids: [17],
    });
    await route.fulfill({
      json: {
        success: [],
        failed: [{ candidate_id: 17, error: "媒体尚未准备好" }],
      },
    });
  });
  await page.goto("/review");
  await page.getByRole("checkbox", { name: "选择候选 17" }).click();
  await page.getByRole("button", { name: "通过并渲染", exact: true }).click();
  await page
    .getByRole("alertdialog")
    .getByRole("button", { name: "确认操作" })
    .click();
  await expect(page.getByText("#17 媒体尚未准备好")).toBeVisible();
  await expect(
    page.getByRole("checkbox", { name: "选择候选 17" }),
  ).toBeChecked();
});

test("a missing QR task stops polling and keeps recovery explicit", async ({
  page,
}) => {
  let polls = 0;
  let starts = 0;
  await page.route("**/api/login/qrcode", async (route) => {
    if (route.request().method() === "POST") starts++;
    await route.fulfill({ json: { task_id: "qr-fixture", status: "scan" } });
  });
  await page.route("**/api/login/qrcode/qr-fixture", async (route) => {
    polls++;
    await route.fulfill({ status: 404, json: { detail: "登录任务不存在" } });
  });
  await page.goto("/settings/login");
  await expect(page.getByRole("button", { name: "重试读取" })).toBeVisible();
  await page.waitForTimeout(4500);
  expect(polls).toBe(1);
  expect(starts).toBe(0);
});

test("plugin settings preserve input kinds and send valid typed values", async ({
  page,
}) => {
  const fields = [
    {
      key: "token",
      label: "访问密钥",
      kind: "password",
      value: "",
      configured: true,
      required: true,
      choices: [],
    },
    {
      key: "title",
      label: "显示名称",
      kind: "text",
      value: "演示",
      choices: [],
    },
    {
      key: "weight",
      label: "权重",
      kind: "number",
      value: 0.5,
      minimum: 0,
      maximum: 1,
      choices: [],
    },
    {
      key: "mode",
      label: "运行方式",
      kind: "select",
      value: "fast",
      choices: ["fast", "accurate"],
    },
    {
      key: "active",
      label: "启用分析",
      kind: "boolean",
      value: true,
      choices: [],
    },
  ];
  await page.route("**/api/plugins/demo/settings", async (route) => {
    if (route.request().method() === "PATCH") {
      expect(route.request().postDataJSON()).toEqual({
        values: {
          title: "新的名称",
          token: "",
          weight: 0.8,
          mode: "accurate",
          active: true,
        },
      });
      await route.fulfill({ json: { plugin: { name: "演示插件" }, fields } });
    } else
      await route.fulfill({ json: { plugin: { name: "演示插件" }, fields } });
  });
  await page.goto("/plugins/demo/settings");
  await page.getByRole("button", { name: "编辑设置", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("显示名称").fill("新的名称");
  await dialog.getByLabel("权重").fill("0.8");
  await dialog.getByLabel("运行方式").selectOption("accurate");
  await dialog.getByRole("button", { name: "保存修改" }).click();
  await expect(dialog).toHaveCount(0);
});

test("Basic media survives worker restart, stays in one tab and stops after logout", async ({
  page,
  context,
}) => {
  await page.unrouteAll();
  await page.goto("/");
  await page.getByLabel("账号", { exact: true }).fill("admin");
  await page
    .getByLabel("密码", { exact: true })
    .fill("browser-fixture-password");
  await page.getByRole("button", { name: "进入工作台" }).click();
  await expect(
    page.getByRole("heading", { name: "工作台概览", exact: true }),
  ).toBeVisible();
  await page.evaluate(() => {
    const image = document.createElement("img");
    image.id = "native-auth-image";
    image.src = "/api/clips/1/cover";
    document.body.append(image);
  });
  await expect(page.locator("#native-auth-image")).toHaveJSProperty(
    "naturalWidth",
    32,
  );
  const cdp = await context.newCDPSession(page);
  const versions = new Map<
    string,
    { runningStatus: string; scriptURL: string }
  >();
  const lifecycle: { versionId: string; status: string; time: number }[] = [];
  cdp.on(
    "ServiceWorker.workerVersionUpdated",
    (event: {
      versions: {
        versionId: string;
        runningStatus: string;
        scriptURL: string;
      }[];
    }) => {
      for (const version of event.versions) {
        versions.set(version.versionId, version);
        if (version.scriptURL.endsWith("/media-auth.js"))
          lifecycle.push({
            versionId: version.versionId,
            status: version.runningStatus,
            time: Date.now(),
          });
      }
    },
  );
  await cdp.send("ServiceWorker.enable");
  const running = () =>
    [...versions].find(
      ([, version]) =>
        version.runningStatus === "running" &&
        version.scriptURL.endsWith("/media-auth.js"),
    );
  await expect.poll(running).toBeTruthy();
  await page.evaluate(() => {
    document.documentElement.dataset.controllerChanges = "0";
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      document.documentElement.dataset.controllerChanges = String(
        Number(document.documentElement.dataset.controllerChanges) + 1,
      );
    });
  });
  const versionId = running()![0];
  const stopIndex = lifecycle.length;
  const transitions = () =>
    lifecycle
      .slice(stopIndex)
      .filter((entry) => entry.versionId === versionId)
      .map((entry) => entry.status);
  await cdp.send("ServiceWorker.stopWorker", { versionId });
  // 后台查询可立即唤醒 Worker，必须验证完整转换，不能采样短暂的停止状态。
  await page.evaluate(async () => {
    await fetch("/auth/status", { cache: "no-store" });
  });
  try {
    await expect.poll(transitions).toContain("stopped");
    await expect
      .poll(() => transitions().slice(transitions().indexOf("stopped") + 1))
      .toContain("running");
  } finally {
    await test.info().attach("service-worker-lifecycle", {
      body: JSON.stringify(lifecycle),
      contentType: "application/json",
    });
  }
  await page.evaluate(() => {
    const image = document.createElement("img");
    image.id = "restarted-auth-image";
    image.src = "/api/clips/1/cover?after-worker-restart";
    document.body.append(image);
  });
  await expect(page.locator("#restarted-auth-image")).toHaveJSProperty(
    "naturalWidth",
    32,
  );
  await expect(page.locator("html")).toHaveAttribute(
    "data-controller-changes",
    "0",
  );
  await cdp.detach();
  const other = await context.newPage();
  await other.goto("/");
  await expect(other.getByRole("heading", { name: "欢迎回来" })).toBeVisible();
  await other.evaluate(() => {
    const image = document.createElement("img");
    image.id = "other-tab-image";
    image.src = "/api/clips/1/cover";
    document.body.append(image);
  });
  await expect(other.locator("#other-tab-image")).toHaveJSProperty(
    "complete",
    true,
  );
  await expect(other.locator("#other-tab-image")).toHaveJSProperty(
    "naturalWidth",
    0,
  );
  await other.close();
  await page.locator('header button[aria-haspopup="menu"]').click();
  await page.getByRole("menuitem", { name: "退出登录", exact: true }).click();
  const denied = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/clips/1/cover?after-logout") &&
      response.status() === 401,
  );
  await page.evaluate(() => {
    const image = document.createElement("img");
    image.id = "logged-out-image";
    image.src = "/api/clips/1/cover?after-logout";
    document.body.append(image);
  });
  await denied;
  await expect(page.locator("#logged-out-image")).toHaveJSProperty(
    "naturalWidth",
    0,
  );
});

test("running room settings omit locked flags while saving other changes", async ({
  page,
}) => {
  const room = {
    id: 1,
    room_id: 22758221,
    uploader_name: "验收主播",
    title: "录制中",
    authorized: true,
    running: true,
    source_available: true,
    recording_state: "recording",
    highlight_threshold: 0.6,
    review_threshold: 0.4,
    auto_approve_threshold: 0.8,
    auto_publish_threshold: 0.9,
    schedule_enabled: true,
    auto_threshold_enabled: true,
    danmaku_sentiment_enabled: true,
    auto_record: false,
    auto_analyze: true,
    auto_render: true,
    auto_approve: false,
    auto_upload: false,
  };
  await page.route("**/api/dashboard", (route) =>
    route.fulfill({ json: { rooms: [room], counts: {} } }),
  );
  await page.route("**/api/rooms/1", async (route) => {
    const body = route.request().postDataJSON();
    expect(body).not.toHaveProperty("schedule_enabled");
    expect(body).not.toHaveProperty("auto_threshold_enabled");
    expect(body).not.toHaveProperty("danmaku_sentiment_enabled");
    expect(body.auto_record).toBe(true);
    await route.fulfill({ json: { status: "updated" } });
  });
  await page.goto("/rooms");
  await page.getByRole("button", { name: "设置", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByRole("switch", { name: "允许预约录制" }),
  ).toBeDisabled();
  await dialog.getByRole("switch", { name: "开播后自动录制" }).click();
  await dialog.getByRole("button", { name: "保存修改" }).click();
  await expect(dialog).toHaveCount(0);
});

test("Lark settings save without sending and only test on explicit action", async ({
  page,
}) => {
  let saved = false;
  let sent = 0;
  await page.route("**/api/settings/configuration", async (route) => {
    if (route.request().method() === "PATCH") {
      expect(route.request().postDataJSON().values).toEqual({
        lark_receive_id: "oc_changed",
      });
      saved = true;
      await route.fulfill({ json: { revision: 2 } });
      return;
    }
    await route.fulfill({
      json: {
        revision: saved ? 2 : 1,
        fields: [
          { key: "lark_app_id", value: "cli_test" },
          {
            key: "lark_app_secret",
            value: null,
            secret: true,
            configured: true,
          },
          { key: "lark_domain", value: "feishu" },
          { key: "lark_receive_id_type", value: "chat_id" },
          {
            key: "lark_receive_id",
            value: saved ? "oc_changed" : "oc_initial",
          },
        ],
      },
    });
  });
  await page.route("**/api/settings/notifications/email", (route) =>
    route.fulfill({ json: { verified: false } }),
  );
  await page.route("**/api/settings/notifications/lark/test", (route) => {
    sent++;
    return route.fulfill({ json: { ok: true } });
  });
  await page.goto("/settings/notifications");
  await expect(page.getByLabel("App Secret", { exact: true })).toHaveValue("");
  await page.getByLabel("接收对象 ID", { exact: true }).fill("oc_changed");
  const testButton = page.getByRole("button", {
    name: "测试发送飞书消息",
    exact: true,
  });
  await expect(testButton).toBeDisabled();
  await page.getByRole("button", { name: "保存设置", exact: true }).click();
  await expect(testButton).toBeEnabled();
  expect(sent).toBe(0);
  await testButton.click();
  await expect(
    page.getByText("飞书测试消息已发送", { exact: true }),
  ).toBeVisible();
  expect(sent).toBe(1);
});
