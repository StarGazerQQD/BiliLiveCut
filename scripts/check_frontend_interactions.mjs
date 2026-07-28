#!/usr/bin/env node
/** Load the real frontend module graph in a deterministic DOM stub and exercise tab switching. */

import assert from "node:assert/strict";
import { cp, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const projectRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const staticRoot = join(projectRoot, "app", "web", "static");

class FakeClassList {
  constructor(initial = []) {
    this.values = new Set(initial);
  }

  add(name) {
    this.values.add(name);
  }

  remove(name) {
    this.values.delete(name);
  }

  contains(name) {
    return this.values.has(name);
  }
}

class FakeElement {
  constructor(id, classes = []) {
    this.id = id;
    this.classList = new FakeClassList(classes);
    this.dataset = {};
    this.style = {};
    this.listeners = new Map();
    this.checked = false;
    this.disabled = false;
    this.innerHTML = "";
    this.textContent = "";
    this.value = "";
  }

  addEventListener(type, listener) {
    this.listeners.set(type, listener);
  }

  async emit(type) {
    const listener = this.listeners.get(type);
    assert.equal(typeof listener, "function", `${this.id} is missing its ${type} handler`);
    await listener({ target: this });
  }

  dispatchEvent(event) {
    const listener = this.listeners.get(event.type);
    if (listener) listener({ target: this });
    return true;
  }

  querySelectorAll() {
    return [];
  }

  querySelector(selector) {
    if (selector === ".empty" && this.innerHTML.includes('class="empty"')) return {};
    return null;
  }

  insertAdjacentHTML(position, html) {
    assert.equal(position, "beforeend");
    this.innerHTML += html;
  }

  getContext() {
    return null;
  }
}

const elements = new Map();
function element(id, classes = []) {
  if (!elements.has(id)) elements.set(id, new FakeElement(id, classes));
  return elements.get(id);
}

const roomsTab = element("rooms-tab", ["tab", "active"]);
roomsTab.dataset.tab = "rooms";
const candidatesTab = element("candidates-tab", ["tab"]);
candidatesTab.dataset.tab = "candidates";
const modelsTab = element("models-tab", ["tab"]);
modelsTab.dataset.tab = "models";
const featuresTab = element("features-tab", ["tab"]);
featuresTab.dataset.tab = "features";
const tabs = [roomsTab, candidatesTab, modelsTab, featuresTab];
const roomsPanel = element("tab-rooms", ["panel", "active"]);
const candidatesPanel = element("tab-candidates", ["panel"]);
const modelsPanel = element("tab-models", ["panel"]);
const featuresPanel = element("tab-features", ["panel"]);
const panels = [roomsPanel, candidatesPanel, modelsPanel, featuresPanel];

const llmDraftFields = new Map([
  [".llm-name", { value: "草稿模型" }],
  [".llm-base", { value: "https://example.invalid/v1" }],
  [".llm-model", { value: "draft-model" }],
  [".llm-key", { value: "draft-secret" }],
  [".llm-search", { value: "" }],
  [".llm-priority", { value: "1" }],
  [".llm-enabled", { checked: true }],
]);
const llmDraftRow = {
  dataset: { id: "" },
  querySelector(selector) {
    return llmDraftFields.get(selector) || null;
  },
};

globalThis.window = globalThis;
globalThis.document = {
  querySelector(selector) {
    return selector.startsWith("#") ? element(selector.slice(1)) : element(selector);
  },
  querySelectorAll(selector) {
    if (selector === ".tab") return tabs;
    if (selector === ".panel") return panels;
    if (selector === ".llm-row" && element("llm-list").innerHTML.includes("llm-row")) {
      return [llmDraftRow];
    }
    return [];
  },
  getElementById(id) {
    return element(id);
  },
  addEventListener(type, listener) {
    if (type === "DOMContentLoaded") listener();
  },
};
globalThis.Event = class Event {
  constructor(type) {
    this.type = type;
  }
};
globalThis.confirm = () => true;
globalThis.setTimeout = () => 0;
globalThis.clearTimeout = () => {};

const requests = [];
const requestDetails = [];
globalThis.fetch = async (path, options = {}) => {
  const requestPath = String(path);
  requests.push(requestPath);
  requestDetails.push({ path: requestPath, options });
  let payload = {};
  if (requestPath === "/api/dashboard") {
    payload = {
      counts: { candidates: 0, clips: 0, active_sessions: 0 },
      modes: ["manual", "semi", "auto"],
      rooms: [],
      sessions: [],
    };
  } else if (requestPath.startsWith("/api/notifications")) {
    payload = [];
  } else if (requestPath.startsWith("/api/candidates")) {
    payload = [];
  } else if (requestPath === "/api/llm-providers") {
    payload = { providers: [], active_count: 0 };
  } else if (requestPath === "/api/llm-providers/test") {
    payload = { results: [{ id: "draft", name: "草稿模型", ok: true, detail: "pong" }] };
  }
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    async json() {
      return payload;
    },
  };
};

async function settle() {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

const temporaryRoot = await mkdtemp(join(tmpdir(), "blc-frontend-smoke-"));
try {
  const copiedStatic = join(temporaryRoot, "static");
  await cp(staticRoot, copiedStatic, { recursive: true });
  await writeFile(join(temporaryRoot, "package.json"), '{"type":"module"}\n', "utf8");

  await import(`${pathToFileURL(join(copiedStatic, "app.js")).href}?smoke=${Date.now()}`);
  await settle();

  assert.equal(element("stat-candidates").textContent, 0, "initial dashboard refresh did not run");
  assert.match(element("rooms-list").innerHTML, /还没有直播间/);
  assert.equal(typeof globalThis.approveCand, "function", "inline review action was not exported to window");
  assert.equal(typeof globalThis.triggerMaintenance, "function", "maintenance action was not exported to window");
  assert.equal(typeof globalThis.saveFeatureSwitches, "function", "feature-switch save action was not exported");
  assert.ok(element("btn-add").listeners.has("click"), "add-room button handler was not registered");

  await candidatesTab.emit("click");
  await settle();

  assert.ok(candidatesTab.classList.contains("active"), "clicked tab did not become active");
  assert.ok(candidatesPanel.classList.contains("active"), "clicked panel did not become active");
  assert.ok(!roomsPanel.classList.contains("active"), "previous panel remained active");
  assert.ok(
    requests.some((path) => path.startsWith("/api/candidates?")),
    "candidate loader did not run after tab click",
  );

  await modelsTab.emit("click");
  await settle();
  const llmList = element("llm-list");
  assert.match(llmList.innerHTML, /尚未配置/);
  const llmLoadsBeforeDraft = requests.filter((path) => path === "/api/llm-providers").length;

  await element("btn-add-llm").emit("click");
  const draftMarkup = llmList.innerHTML;
  assert.match(draftMarkup, /llm-row/, "add-model action did not create a draft row");

  await modelsTab.emit("click");
  await settle();
  assert.equal(llmList.innerHTML, draftMarkup, "periodic refresh discarded the unsaved model draft");
  assert.equal(
    requests.filter((path) => path === "/api/llm-providers").length,
    llmLoadsBeforeDraft,
    "dirty model form should not be reloaded from the server",
  );

  await element("btn-test-llm").emit("click");
  await settle();
  const testRequest = requestDetails.find((entry) => entry.path === "/api/llm-providers/test");
  assert.ok(testRequest, "model connectivity test was not requested");
  const testPayload = JSON.parse(testRequest.options.body || "null");
  assert.equal(testPayload.providers[0].api_key, "draft-secret", "connectivity test ignored draft API key");
  assert.match(element("llm-test-results").innerHTML, /pong/, "connectivity result detail was not rendered");

  console.log(
    "PASS: frontend module graph, bindings, initial refresh and tab interaction; feature switches, draft preservation and draft connectivity test",
  );
} finally {
  await rm(temporaryRoot, { recursive: true, force: true });
}
