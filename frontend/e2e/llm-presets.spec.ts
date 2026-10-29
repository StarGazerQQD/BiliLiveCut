import { test, expect, type Page } from "@playwright/test";

type Provider = Record<string, unknown>;

async function fixture(page: Page, introduceConflict = false) {
  let providers: Provider[] = [
    {
      id: "existing-service",
      name: "原有服务",
      base_url: "https://existing.example.invalid/v1",
      model: "existing-model",
      api_key_set: true,
      enabled: true,
      priority: 10,
      web_search_param: "",
      price_input_per_m: 1,
      price_output_per_m: 2,
    },
  ];
  let revision = 1;
  let routes: Provider[] = [
    {
      id: "existing-route",
      name: "原分析",
      capability: "analysis",
      connection_id: "existing-service",
      model: "existing-model",
      enabled: true,
      priority: 10,
      web_search_param: "",
      price_input_per_m: 1,
      price_output_per_m: 2,
    },
  ];
  const writes: {
    path: string;
    connections: Provider[];
    routes: Provider[];
    route_id?: string;
  }[] = [];
  await page.route("**/*", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/auth/status")
      return route.fulfill({ json: { enabled: false } });
    if (path.startsWith("/api/model-configuration")) {
      if (request.method() !== "GET") {
        const body = request.postDataJSON();
        const incoming = body.connections as Provider[];
        writes.push({ path, ...body });
        if (introduceConflict && request.method() === "PUT") {
          introduceConflict = false;
          revision++;
          routes.push({
            ...routes[0],
            id: "other-user-route",
            model: "other-user-model",
          });
        }
        if (body.revision !== revision)
          return route.fulfill({
            status: 409,
            json: { detail: "配置已在其他页面修改" },
          });
        if (path.endsWith("/test"))
          return route.fulfill({
            json: {
              id: body.route_id,
              ok: true,
              detail: "本地测试响应",
            },
          });
        providers = incoming.map(({ api_key, clear_api_key, ...provider }) => ({
          ...provider,
          api_key_set:
            !clear_api_key && (!!api_key || provider.id === "existing-service"),
        }));
        routes = body.routes;
        revision++;
      }
      return route.fulfill({
        json: {
          revision,
          source: "routes",
          connections: providers,
          routes,
          asr_selection: "",
          active_counts: { analysis: routes.length },
          model_capacities: [
            {
              id: "deepseek",
              base_urls: ["https://api.deepseek.com"],
              models: ["deepseek-flash"],
              context_window_tokens: 1000000,
              max_input_tokens: null,
              max_output_tokens: 393216,
              sources: [
                "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
              ],
              checked_on: "2026-10-29",
              note: "输出包含思考",
            },
          ],
        },
      });
    }
    if (path.startsWith("/api/")) return route.fulfill({ json: [] });
    return route.continue();
  });
  return writes;
}

test("large custom limits persist and official capacity follows the edited endpoint", async ({
  page,
}) => {
  const writes = await fixture(page);
  await page.goto("/settings/llm");
  await expect(page.getByLabel("模型容量")).toContainText("型号容量未核实");
  await page
    .getByLabel("路由上下文 Token 上限", { exact: true })
    .fill("1048576");
  await page.getByLabel("路由输入 Token 上限", { exact: true }).fill("800000");
  await page.getByLabel("路由输出 Token 上限", { exact: true }).fill("393216");
  await page
    .getByRole("button", { name: "保存连接与路由", exact: true })
    .click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0].routes[0]).toMatchObject({
    context_window_tokens: 1048576,
    max_input_tokens: 800000,
    max_output_tokens: 393216,
  });
  await page.reload();
  await expect(
    page.getByLabel("路由输出 Token 上限", { exact: true }),
  ).toHaveValue("393216");
  await page.getByLabel("模型名称", { exact: false }).fill("deepseek-flash");
  await page
    .getByLabel("API 地址", { exact: false })
    .fill("https://API.DEEPSEEK.COM:443");
  await expect(page.getByLabel("模型容量")).toContainText("输出包含思考");
  await page.getByLabel("路由输出 Token 上限", { exact: true }).fill("4096");
  await expect(page.getByLabel("模型容量")).toContainText("4,096");
  await page
    .getByLabel("业务能力", { exact: false })
    .last()
    .selectOption("asr");
  await expect(
    page.getByLabel("路由输出 Token 上限", { exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "保存连接与路由", exact: true })
    .click();
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1].routes[0]).toMatchObject({
    context_window_tokens: null,
    max_input_tokens: null,
    max_output_tokens: null,
  });
});

test("provider presets create editable drafts without reusing existing credentials", async ({
  page,
}) => {
  const writes = await fixture(page);
  await page.goto("/settings/llm");
  const picker = page.getByLabel("服务商预设", { exact: true });
  for (const name of [
    "DeepSeek",
    "阿里云百炼（Qwen）",
    "火山方舟（豆包）",
    "Kimi（Moonshot）",
    "智谱 GLM",
    "小米 MiMo",
    "硅基流动",
    "MiniMax",
    "百度千帆",
    "腾讯云 TokenHub（混元等）",
    "讯飞星火",
    "魔搭 ModelScope",
    "阶跃星辰 StepFun",
    "无问芯穹",
    "腾讯混元（旧平台）",
    "腾讯云 LKEAP（旧平台）",
  ]) {
    await expect(picker.getByRole("option", { name, exact: true })).toHaveCount(
      1,
    );
  }
  for (const [id, name, url, model] of [
    ["deepseek", "DeepSeek", "https://api.deepseek.com", "deepseek-flash"],
    [
      "bailian",
      "阿里云百炼（Qwen）",
      "https://dashscope.aliyuncs.com/compatible-mode/v1",
      "qwen-plus",
    ],
    [
      "volcengine",
      "火山方舟（豆包）",
      "https://ark.cn-beijing.volces.com/api/v3",
      "doubao-seed-2-1-pro-260915",
    ],
  ]) {
    await picker.selectOption(id);
    await expect(
      page.getByRole("link", { name: `${name}官方接入文档` }),
    ).toHaveAttribute("rel", "noopener noreferrer");
    await page.getByRole("button", { name: "添加服务", exact: true }).click();
    await expect(
      page.getByLabel("服务名称", { exact: true }).last(),
    ).toHaveValue(name);
    await expect(
      page.getByLabel("API 地址", { exact: false }).last(),
    ).toHaveValue(url);
    await page.getByRole("button", { name: "添加路由", exact: true }).click();
    await expect(
      page.getByLabel("模型名称", { exact: false }).last(),
    ).toHaveValue(model);
    await expect(
      page.getByLabel("API 密钥", { exact: true }).last(),
    ).toHaveValue("");
  }
  expect(writes).toEqual([]);
  await expect(
    page.getByLabel("API 地址", { exact: false }).first(),
  ).toHaveValue("https://existing.example.invalid/v1");
  await page
    .getByLabel("API 地址", { exact: false })
    .last()
    .fill("https://customized.example.invalid/v1");
  await page
    .getByLabel("模型名称", { exact: false })
    .last()
    .fill("my-endpoint");
  await page
    .getByLabel("API 密钥", { exact: true })
    .last()
    .fill("new-test-key");
  await page
    .getByRole("button", { name: "测试此路由", exact: true })
    .last()
    .click();
  await expect(page.getByText("本地测试响应")).toHaveCount(1);
  expect(writes.map((write) => write.path)).toEqual([
    "/api/model-configuration/test",
  ]);
  expect(writes[0].connections[0]).toMatchObject({
    id: "existing-service",
    api_key: "",
    clear_api_key: false,
    base_url: "https://existing.example.invalid/v1",
  });
  expect(writes[0].connections.at(-1)).toMatchObject({
    base_url: "https://customized.example.invalid/v1",
    api_key: "new-test-key",
  });
  expect(writes[0].routes.at(-1)).toMatchObject({
    model: "my-endpoint",
    connection_id: writes[0].connections.at(-1)?.id,
  });
  expect(writes[0].route_id).toBe(writes[0].routes.at(-1)?.id);
  expect(writes[0].connections[1]).not.toHaveProperty("docsUrl");
  expect(writes[0].connections[1]).not.toHaveProperty("model");
  await page
    .getByRole("button", { name: "保存连接与路由", exact: true })
    .click();
  await expect(
    page.getByText("连接与业务路由已保存；新任务使用新配置"),
  ).toBeVisible();
  expect(writes.map((write) => write.path)).toEqual([
    "/api/model-configuration/test",
    "/api/model-configuration",
  ]);
  await expect(page.getByLabel("API 密钥", { exact: true }).last()).toHaveValue(
    "",
  );
});

test("account-specific models stay blank and custom drafts can be discarded", async ({
  page,
}) => {
  const writes = await fixture(page);
  await page.goto("/settings/llm");
  await page.getByLabel("服务商预设", { exact: true }).selectOption("qianfan");
  await page.getByRole("button", { name: "添加服务", exact: true }).click();
  await page.getByRole("button", { name: "添加路由", exact: true }).click();
  await expect(
    page.getByLabel("模型名称", { exact: false }).last(),
  ).toHaveValue("");
  await page.getByLabel("服务商预设", { exact: true }).selectOption("custom");
  await page.getByRole("button", { name: "添加服务", exact: true }).click();
  await expect(page.getByLabel("服务名称", { exact: true }).last()).toHaveValue(
    "",
  );
  await expect(
    page.getByLabel("API 地址", { exact: false }).last(),
  ).toHaveValue("");
  await page.getByRole("button", { name: "放弃全部修改", exact: true }).click();
  await expect(page.getByLabel("服务名称", { exact: true })).toHaveCount(1);
  expect(writes).toEqual([]);
});

test("independent summary and ASR routes share one connection without copying its key", async ({
  page,
}) => {
  const writes = await fixture(page);
  await page.goto("/settings/llm");
  await page
    .getByLabel("使用连接", { exact: true })
    .selectOption("existing-service");
  for (const capability of ["summary", "asr"]) {
    await page
      .getByLabel("业务能力", { exact: true })
      .first()
      .selectOption(capability);
    await page.getByRole("button", { name: "添加路由", exact: true }).click();
    await page
      .getByLabel("模型名称", { exact: false })
      .last()
      .fill(`${capability}-model`);
  }
  await expect(
    page.getByRole("button", { name: "测试此路由", exact: true }),
  ).toHaveCount(2);
  await expect(page.getByLabel("API 密钥", { exact: true })).toHaveCount(1);
  await page
    .getByRole("button", { name: "保存连接与路由", exact: true })
    .click();
  await expect(
    page.getByText("连接与业务路由已保存；新任务使用新配置"),
  ).toBeVisible();
  expect(writes).toHaveLength(1);
  expect(writes[0].connections).toHaveLength(1);
  expect(writes[0].connections[0].api_key).toBe("");
  expect(writes[0].routes.slice(1)).toMatchObject([
    {
      capability: "summary",
      model: "summary-model",
      connection_id: "existing-service",
    },
    {
      capability: "asr",
      model: "asr-model",
      connection_id: "existing-service",
      web_search_param: "",
      price_input_per_m: 0,
      price_output_per_m: 0,
    },
  ]);
  expect(writes[0].routes.every((route) => !("api_key" in route))).toBe(true);
});

test("model catalog conflicts preserve the draft revision and the other editor's route", async ({
  page,
}) => {
  const writes = await fixture(page, true);
  await page.goto("/settings/llm");
  await page.getByLabel("服务名称", { exact: true }).fill("本地草稿");
  await page
    .getByRole("button", { name: "保存连接与路由", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("配置已在其他页面修改");
  await page.getByRole("button", { name: "读取最新版本", exact: true }).click();
  await expect(
    page.getByText(/已读取最新配置，当前草稿与原版本仍保留/),
  ).toBeVisible();
  await expect(page.getByLabel("服务名称", { exact: true })).toHaveValue(
    "本地草稿",
  );
  await page
    .getByRole("button", { name: "保存连接与路由", exact: true })
    .click();
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1]).toMatchObject({ revision: 1 });
  await page.getByRole("button", { name: "放弃全部修改", exact: true }).click();
  await expect(
    page.getByLabel("模型名称", { exact: false }).last(),
  ).toHaveValue("other-user-model");
  await page.getByLabel("服务名称", { exact: true }).fill("重新修改");
  await page
    .getByRole("button", { name: "保存连接与路由", exact: true })
    .click();
  await expect(
    page.getByText("连接与业务路由已保存；新任务使用新配置"),
  ).toBeVisible();
  expect(writes[2]).toMatchObject({ revision: 2 });
  expect(writes[2].routes.at(-1)).toMatchObject({
    id: "other-user-route",
    model: "other-user-model",
  });
});
