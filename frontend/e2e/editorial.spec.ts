import { test, expect, type Page } from "@playwright/test";

const view = {
  schema_version: "editorial-view-v1",
  title: "主播解释比赛规则",
  summary: "每轮计分和胜负说明",
  explanation_state: "available",
  editorial_state: "not_run",
  evidence: [],
  hotspot_event_id: 81,
  candidate_id: 22,
  event_id: 17,
  technical_score: null,
};

async function basicRoutes(page: Page) {
  await page.route("**/*", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (!/^\/(api|auth|review\/api)/.test(path)) return route.continue();
    if (path === "/auth/status")
      return route.fulfill({ json: { enabled: false } });
    if (path === "/api/dashboard")
      return route.fulfill({ json: { rooms: [], counts: {} } });
    if (path === "/api/events") return route.fulfill({ status: 204 });
    if (path === "/api/jobs") return route.fulfill({ json: { jobs: [] } });
    if (path === "/api/sessions/7/timeline")
      return route.fulfill({
        json: {
          session: { session_id: 7, title: "测试场次", status: "stopped" },
          counts: { visible: 1, rejected: 0 },
          points: [
            {
              key: "hotspot:81",
              point_type: "hotspot",
              hotspot_event_id: 81,
              candidate_id: 22,
              event_id: 17,
              title: view.title,
              summary: view.summary,
              hotspot_duration_s: 20,
              clip_duration_s: 6,
              review_url: "/review/22",
              editorial: view,
            },
          ],
        },
      });
    if (path === "/review/api/22")
      return route.fulfill({
        json: {
          candidate: {
            id: 22,
            start_ts: "2026-10-26T10:00:00",
            end_ts: "2026-10-26T10:00:06",
          },
          viewer: { actor: "本机管理员", role: "admin", blinded: false },
          editorial: view,
          workflow: {
            claim: { active: true, claimed_by: "本机管理员" },
            draft: { reason: "服务端备注" },
          },
        },
      });
    return route.fulfill({ json: [] });
  });
}

test("previews exact targets before submitting and restores the persisted job", async ({
  page,
}) => {
  await basicRoutes(page);
  const submissions: unknown[] = [];
  const selections: { keys: string[] }[] = [];
  await page.route(
    /\/api\/sessions\/7\/editorial-reanalysis(?:\/preview)?$/,
    async (route) => {
      const request = route.request();
      const selection = request.postDataJSON();
      if (new URL(request.url()).pathname.endsWith("/preview")) {
        selections.push(selection);
        const keys = selection.keys.length
          ? selection.keys
          : ["hotspot:81", "hotspot:82", "hotspot:83"];
        return route.fulfill({
          json: {
            session_id: 7,
            total: keys.length,
            next_offset: null,
            fingerprint: "a".repeat(64),
            targets: keys.map((key: string) => ({
              key,
              verdict: "unknown",
              protection: key === "hotspot:83" ? "human_decision" : null,
            })),
          },
        });
      }
      submissions.push(selection);
      return route.fulfill({
        status: 202,
        json: {
          job_id: "editorial-job",
          status: "queued",
          status_url: "/api/jobs/editorial-job",
        },
      });
    },
  );
  await page.route("**/api/jobs/editorial-job", (route) =>
    route.fulfill({
      json: {
        id: "editorial-job",
        status: "succeeded",
        message: "重新分析完成",
        result: {
          updated: 1,
          skipped: 0,
          targets: [{ key: "hotspot:81", status: "updated" }],
        },
      },
    }),
  );
  await page.goto("/sessions?session=7");
  await expect(page.getByText(/热点跨度 00:20/)).toBeVisible();
  await expect(page.getByText(/实际成片 00:06/)).toBeVisible();
  expect(submissions).toHaveLength(0);
  expect(selections).toHaveLength(0);
  await page.getByRole("button", { name: "预览分析对象" }).click();
  await expect(page.getByLabel("选择 hotspot:83")).toBeDisabled();
  await page.getByLabel("选择 hotspot:82").uncheck();
  await expect(
    page.getByRole("button", { name: "确认重新分析" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "更新所选对象预览" }).click();
  await expect(
    page.getByRole("button", { name: "确认重新分析" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "确认重新分析" }).click();
  expect(submissions).toHaveLength(0);
  await page.getByRole("button", { name: "确认操作", exact: true }).click();
  await expect(page.getByText("已更新 1 项，跳过 0 项")).toBeVisible();
  expect(submissions).toEqual([
    {
      selection: {
        session_id: 7,
        keys: ["hotspot:81"],
        verdicts: [],
        failed_only: false,
        offset: 0,
        limit: 50,
      },
      fingerprint: "a".repeat(64),
    },
  ]);
  expect(selections).toHaveLength(2);
});

test("refresh restores editorial HTTP state without overwriting unsaved review notes", async ({
  page,
}) => {
  await basicRoutes(page);
  let release: (() => void) | undefined;
  const ready = new Promise<void>((resolve) => {
    release = resolve;
  });
  let streams = 0;
  let reads = 0;
  let changed = false;
  let writes = 0;
  await page.route("**/api/events", async (route) => {
    if (++streams > 1) return route.fulfill({ status: 204 });
    await ready;
    return route.fulfill({
      contentType: "text/event-stream",
      body: `id: ${"b".repeat(32)}:1\nevent: invalidate\ndata: {"version":1,"topics":["state"]}\n\n`,
    });
  });
  await page.route("**/review/api/22", (route) => {
    if (route.request().method() !== "GET") writes++;
    reads++;
    return route.fulfill({
      json: {
        candidate: {
          id: 22,
          start_ts: "2026-10-26T10:00:00",
          end_ts: "2026-10-26T10:00:06",
        },
        viewer: { actor: "本机管理员", role: "admin", blinded: false },
        editorial: {
          ...view,
          summary: changed ? "更新后的解释" : view.summary,
        },
        workflow: {
          claim: { active: true, claimed_by: "本机管理员" },
          draft: { reason: changed ? "新服务端备注" : "旧服务端备注" },
        },
      },
    });
  });
  try {
    await page.goto("/review/22");
    await expect(page.getByRole("heading", { name: view.title })).toBeVisible();
    await page.getByLabel("审核备注", { exact: false }).fill("我的未提交判断");
    await expect.poll(() => streams).toBe(1);
    const before = reads;
    changed = true;
    release?.();
    await expect.poll(() => reads, { timeout: 2500 }).toBeGreaterThan(before);
    await expect(page.getByText("更新后的解释")).toBeVisible();
    await expect(page.getByLabel("审核备注", { exact: false })).toHaveValue(
      "我的未提交判断",
    );
    expect(writes).toBe(0);
  } finally {
    release?.();
  }
});
