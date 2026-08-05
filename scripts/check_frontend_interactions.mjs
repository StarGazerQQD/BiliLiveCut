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

  async emit(type, target = this) {
    const listener = this.listeners.get(type);
    assert.equal(typeof listener, "function", `${this.id} is missing its ${type} handler`);
    await listener({ target });
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
let dashboardRooms = [];
const sessionTimelineRows = [{
  session_id: 21,
  room_db_id: 1,
  room_id: 23771139,
  source_label: "测试主播 · 房间 23771139",
  status: "finished",
  started_at_gmt8: "2026-08-05T19:00:00+08:00",
  ended_at_gmt8: "2026-08-05T20:00:00+08:00",
  duration_s: 3600,
  segment_count: 12,
  highlight_count: 2,
  pending_review_count: 1,
  rejected_count: 1,
  processing_state: "ready",
}];
globalThis.fetch = async (path, options = {}) => {
  const requestPath = String(path);
  requests.push(requestPath);
  requestDetails.push({ path: requestPath, options });
  let payload = {};
  if (requestPath === "/api/dashboard") {
    payload = {
      counts: { candidates: 0, clips: 0, active_sessions: 0 },
      modes: ["manual", "semi", "auto"],
      rooms: dashboardRooms,
      sessions: [],
    };
  } else if (requestPath.startsWith("/api/notifications")) {
    payload = [];
  } else if (requestPath.startsWith("/api/sessions/timeline")) {
    payload = sessionTimelineRows;
  } else if (requestPath.startsWith("/api/sessions/21/timeline")) {
    payload = {
      session: sessionTimelineRows[0],
      timezone: "GMT+8",
      counts: { visible: 1, rejected: 0, total: 1 },
      points: [{
        candidate_id: 31,
        clock_gmt8: "19:45:10",
        start_at_gmt8: "2026-08-05T19:44:30+08:00",
        end_at_gmt8: "2026-08-05T19:46:00+08:00",
        duration_s: 90,
        summary: "测试高光梗概",
        representative_danmaku: [{ text: "名场面", count: 5 }],
        confidence: 0.88,
        source_signals: ["danmaku", "transcript"],
        review_status: "pending",
        rejected: false,
        review_url: "/review/31",
        provenance: { rule_score: 0.8, llm_score: 0.9, highlight_score: 0.88, dynamic_bounds: true, cross_segment: true, danmaku_lag_s: 7.5 },
      }],
    };
  } else if (requestPath === "/api/sessions/21/reanalyze") {
    payload = { session_id: 21, requested: true };
  } else if (requestPath === "/api/llm-providers") {
    payload = { providers: [], active_count: 0 };
  } else if (requestPath === "/api/llm-providers/test") {
    payload = { results: [{ id: "draft", name: "草稿模型", ok: true, detail: "pong" }] };
  } else if (requestPath === "/api/rooms/1/start") {
    dashboardRooms[0].running = true;
    dashboardRooms[0].recording_state = "starting";
    dashboardRooms[0].active_session_id = 7;
    payload = { status: "started" };
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
  assert.equal(typeof globalThis.saveGlobalFeatureSettings, "function", "global feature save action was not exported");
  assert.equal(typeof globalThis.toggleSessionTimeline, "function", "timeline expand action was not exported");
  assert.equal(typeof globalThis.correctTranscript, "function", "transcript correction action was not exported");
  assert.ok(element("btn-add").listeners.has("click"), "add-room button handler was not registered");

  dashboardRooms = [{
    id: 1,
    title: "草稿保护测试",
    input_url: "1",
    room_id: 1,
    authorized: true,
    running: false,
    recording_state: "stopped",
    room_config: {},
    mode: "manual",
    highlight_threshold: 0.6,
    auto_publish_threshold: 0.8,
    schedule_enabled: false,
    auto_threshold_enabled: false,
    danmaku_sentiment_enabled: true,
  }];
  await roomsTab.emit("click");
  await settle();
  const roomsMarkupBeforeDraft = element("rooms-list").innerHTML;
  assert.match(roomsMarkupBeforeDraft, /草稿保护测试/);
  const dirtyRoomControl = {
    closest(selector) {
      assert.equal(selector, "[data-room-dirty-section]");
      return { dataset: { roomDirtySection: "controls:1" } };
    },
  };
  await element("rooms-list").emit("input", dirtyRoomControl);
  dashboardRooms[0].title = "服务器刷新后的标题";
  await roomsTab.emit("click");
  await settle();
  assert.equal(
    element("rooms-list").innerHTML,
    roomsMarkupBeforeDraft,
    "periodic refresh discarded unsaved room recording options",
  );
  assert.equal(element("rooms-dirty-hint").style.display, "", "dirty room hint was not shown");

  assert.equal(typeof globalThis.startRoom, "function", "start-room action was not exported");
  await globalThis.startRoom(1);
  await settle();
  assert.equal(
    element("rooms-list").innerHTML,
    roomsMarkupBeforeDraft,
    "start-room refresh discarded unsaved room recording options",
  );
  assert.match(element("room-status-1").innerHTML, /starting/, "start-room did not refresh runtime status");
  assert.match(element("room-meta-1").textContent, /#7/, "start-room did not refresh active session metadata");
  assert.match(element("room-actions-1").innerHTML, /stopRoom\(1\)/, "start-room did not render stop actions");
  assert.doesNotMatch(element("room-actions-1").innerHTML, /startRoom\(1\)/, "start action remained visible");
  assert.equal(element("room-lock-hint-1").textContent, "(\u5f55\u5236\u4e2d\u9501\u5b9a)");
  for (const id of ["sw-se-1", "sw-at-1", "sw-ds-1"]) {
    assert.equal(element(id).disabled, true, `${id} remained editable after recording started`);
  }

  element("mode-1").value = "semi";
  element("ht-1").value = "0.72";
  element("at-1").value = "0.84";
  for (const id of ["sw-se-1", "sw-at-1", "sw-ds-1"]) {
    element(id).checked = true;
    element(id).disabled = true;
  }
  const roomSaveRequestOffset = requestDetails.length;
  await globalThis.saveRoom(1);
  await settle();
  const roomSaveRequest = requestDetails
    .slice(roomSaveRequestOffset)
    .find((entry) => entry.path === "/api/rooms/1" && entry.options.method === "PATCH");
  assert.ok(roomSaveRequest, "room settings save was not requested");
  const roomSavePayload = JSON.parse(roomSaveRequest.options.body || "null");
  assert.equal(roomSavePayload.mode, "semi");
  assert.ok(!("schedule_enabled" in roomSavePayload), "room save submitted a locked schedule switch");
  assert.ok(!("auto_threshold_enabled" in roomSavePayload), "room save submitted a locked threshold switch");
  assert.ok(!("danmaku_sentiment_enabled" in roomSavePayload), "room save submitted a locked sentiment switch");

  for (const id of ["feature-record-1", "feature-analyze-1", "feature-render-1", "feature-approve-1", "feature-upload-1"]) {
    element(id).checked = true;
  }
  for (const id of ["feature-schedule-1", "feature-threshold-1", "feature-sentiment-1"]) {
    element(id).checked = true;
    element(id).disabled = true;
  }
  element("feature-approve-threshold-1").value = "0.88";
  element("feature-review-threshold-1").value = "0.56";
  const featureSaveRequestOffset = requestDetails.length;
  await globalThis.saveFeatureSwitches(1);
  await settle();
  const featureSaveRequest = requestDetails
    .slice(featureSaveRequestOffset)
    .find((entry) => entry.path === "/api/rooms/1" && entry.options.method === "PATCH");
  assert.ok(featureSaveRequest, "feature settings save was not requested");
  const featureSavePayload = JSON.parse(featureSaveRequest.options.body || "null");
  assert.equal(featureSavePayload.auto_record, true);
  assert.ok(!("schedule_enabled" in featureSavePayload), "feature save submitted a locked schedule switch");
  assert.ok(!("auto_threshold_enabled" in featureSavePayload), "feature save submitted a locked threshold switch");
  assert.ok(!("danmaku_sentiment_enabled" in featureSavePayload), "feature save submitted a locked sentiment switch");

  await candidatesTab.emit("click");
  await settle();

  assert.ok(candidatesTab.classList.contains("active"), "clicked tab did not become active");
  assert.ok(candidatesPanel.classList.contains("active"), "clicked panel did not become active");
  assert.ok(!roomsPanel.classList.contains("active"), "previous panel remained active");
  assert.ok(
    requests.some((path) => path.startsWith("/api/sessions/timeline")),
    "session timeline loader did not run after tab click",
  );
  assert.match(element("timeline-list").innerHTML, /测试主播/);
  assert.match(element("timeline-list").innerHTML, /会话 #21/);

  await globalThis.toggleSessionTimeline(21);
  await settle();
  assert.match(element("timeline-detail-21").innerHTML, /测试高光梗概/);
  assert.match(element("timeline-detail-21").innerHTML, /名场面/);
  assert.match(element("timeline-detail-21").innerHTML, /跨片段/);

  const reanalysisRequestOffset = requestDetails.length;
  await globalThis.requestSessionReanalysis(21, false);
  await settle();
  const reanalysisRequest = requestDetails
    .slice(reanalysisRequestOffset)
    .find((entry) => entry.path === "/api/sessions/21/reanalyze" && entry.options.method === "POST");
  assert.ok(reanalysisRequest, "session reanalysis was not requested");
  assert.equal(JSON.parse(reanalysisRequest.options.body).retranscribe, false);

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
    "PASS: frontend module graph, bindings, session timeline expansion/reanalysis, room/model draft preservation, locked room switches, feature switches and draft connectivity test",
  );
} finally {
  await rm(temporaryRoot, { recursive: true, force: true });
}
