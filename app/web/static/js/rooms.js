// BiliLiveCut 直播间管理:列表、添加、开关、预约、主题、阈值学习
import { $, api, toast, esc, badge } from "./common.js";

// ----------------------------- 渲染:直播间 ----------------------------- //
async function loadRooms() {
  const [data, mlStatus] = await Promise.all([
    api("GET", "/api/dashboard"),
    api("GET", "/api/highlight-ml/status"),
  ]);
  const mlStatusEl = $("#highlight-ml-status");
  if (mlStatusEl) {
    const roles = mlStatus.available
      ? `Champion v${mlStatus.champion_version}${mlStatus.shadow_version ? ` · Shadow v${mlStatus.shadow_version}` : ""}`
      : "尚无 Champion";
    mlStatusEl.textContent = `全局模式: ${mlStatus.mode} · ${roles} · Schema ${mlStatus.schema_version}`;
  }
  $("#stat-candidates").textContent = data.counts.candidates;
  $("#stat-clips").textContent = data.counts.clips;
  $("#stat-sessions").textContent = data.counts.active_sessions;

  const modes = data.modes;
  const html = data.rooms.map((r) => `
    <div class="item">
      <div class="head">
        <div>
          <div class="title">${esc(r.title || r.input_url)} ${badge(r.recording_state || (r.running ? "running" : "stopped"))}</div>
          <div class="sub">db_id=${r.id} \u00b7 room_id=${r.room_id ?? "-"} \u00b7 \u6388\u6743:${r.authorized ? "\u662f" : "\u5426"}${r.active_session_id ? ` · 会话 #${r.active_session_id}` : ""}</div>
        </div>
        <div class="actions">
          ${r.running
            ? `<button class="ok" onclick="markHighlight(${r.id})">高光打点</button>
               <button class="danger" onclick="stopRoom(${r.id})">停止并收尾</button>
               <button onclick="stopRoom(${r.id}, true)">强制停止</button>`
            : r.room_config.recording_paused
              ? `<button class="ok" onclick="resumeRoom(${r.id})">恢复录制</button>`
              : `<button class="ok" onclick="startRoom(${r.id})">\u5f00\u59cb\u5f55\u5236</button>`}
        </div>
      </div>
      <div class="thresholds">
        <label>\u5ba1\u6838\u6a21\u5f0f
          <select id="mode-${r.id}">
            ${modes.map((m) => `<option value="${m}" ${m === r.mode ? "selected" : ""}>${m}</option>`).join("")}
          </select>
        </label>
        <label>\u9ad8\u5149\u9608\u503c
          <input type="number" step="0.05" min="0" max="1" id="ht-${r.id}" value="${r.highlight_threshold}" />
        </label>
        <label>\u81ea\u52a8\u53d1\u5e03\u9608\u503c
          <input type="number" step="0.05" min="0" max="1" id="at-${r.id}" value="${r.auto_publish_threshold}" />
        </label>
        <button onclick="saveRoom(${r.id})">\u4fdd\u5b58</button>
      </div>
      <div class="thresholds" style="margin-top: 6px">
        <label class="switch-row">
          <input type="checkbox" id="sw-se-${r.id}" ${r.schedule_enabled ? "checked" : ""} ${r.running ? "disabled" : ""} />
          \u9884\u7ea6\u5f55\u5236
        </label>
        <label class="switch-row">
          <input type="checkbox" id="sw-at-${r.id}" ${r.auto_threshold_enabled ? "checked" : ""} ${r.running ? "disabled" : ""} />
          \u9608\u503c\u81ea\u5b66\u4e60
        </label>
        <label class="switch-row">
          <input type="checkbox" id="sw-ds-${r.id}" ${r.danmaku_sentiment_enabled ? "checked" : ""} ${r.running ? "disabled" : ""} />
          \u5f39\u5e55\u60c5\u7eea
        </label>
        <label>高光模型
          <select id="ml-${r.id}">
            ${["inherit", "off", "shadow", "champion"].map((mode) =>
              `<option value="${mode}" ${mode === (r.room_config.highlight_ml_mode || "inherit") ? "selected" : ""}>${mode}</option>`
            ).join("")}
          </select>
        </label>
        ${r.running ? '<span class="muted">(\u5f55\u5236\u4e2d\u9501\u5b9a)</span>' : ""}
      </div>
      <details class="room-config-detail" style="margin-top:8px">
        <summary style="font-size:12px;color:var(--muted);cursor:pointer">\u623f\u95f4\u914d\u7f6e(\u70ed\u8bcd/\u522b\u540d/\u5c4f\u853d)</summary>
        <div class="thresholds" style="margin-top:6px;flex-direction:column;align-items:stretch">
          <label>\u70ed\u8bcd(\u6362\u884c\u5206\u9694)
            <textarea id="hw-${r.id}" rows="2" style="width:100%;font-size:11px">${(r.room_config.hotwords||[]).join("\n")}</textarea>
          </label>
          <label>\u9ad8\u5149\u5173\u952e\u8bcd(\u6362\u884c\u5206\u9694)
            <textarea id="hk-${r.id}" rows="2" style="width:100%;font-size:11px">${(r.room_config.highlight_keywords||[]).join("\n")}</textarea>
          </label>
          <label>\u522b\u540d(\u6bcf\u884c:\u9519\u8bef=\u6b63\u786e)
            <textarea id="al-${r.id}" rows="2" style="width:100%;font-size:11px">${Object.entries(r.room_config.aliases||{}).map(([k,v])=>`${k}=${v}`).join("\n")}</textarea>
          </label>
          <label>\u5c4f\u853d\u8bdd\u9898(\u6362\u884c\u5206\u9694)
            <textarea id="bt-${r.id}" rows="2" style="width:100%;font-size:11px">${(r.room_config.blocked_topics||[]).join("\n")}</textarea>
          </label>
          <button onclick="saveRoomConfig(${r.id})" style="margin-top:4px">\u4fdd\u5b58\u623f\u95f4\u914d\u7f6e</button>
        </div>
      </details>
      ${r.auto_threshold_enabled ? `<div id="tl-${r.id}" class="threshold-learning"></div>` : ""}
    </div>`).join("");
  $("#rooms-list").innerHTML = html || `<div class="empty">\u8fd8\u6ca1\u6709\u76f4\u64ad\u95f4,\u5148\u5728\u4e0a\u65b9\u6dfb\u52a0\u3002</div>`;

  // \u5f02\u6b65\u52a0\u8f7d\u9608\u503c\u5b66\u4e60\u6458\u8981\u3002
  data.rooms.forEach((r) => {
    if (r.auto_threshold_enabled) loadThresholdLearning(r.id);
  });
}

async function saveRoom(id) {
  try {
    await api("PATCH", `/api/rooms/${id}`, {
      mode: $(`#mode-${id}`).value,
      highlight_threshold: parseFloat($(`#ht-${id}`).value),
      auto_publish_threshold: parseFloat($(`#at-${id}`).value),
      schedule_enabled: ($(`#sw-se-${id}`) || {}).checked,
      auto_threshold_enabled: ($(`#sw-at-${id}`) || {}).checked,
      danmaku_sentiment_enabled: ($(`#sw-ds-${id}`) || {}).checked,
    });
    toast("\u5df2\u4fdd\u5b58\u9608\u503c/\u6a21\u5f0f");
  } catch (e) { toast("\u4fdd\u5b58\u5931\u8d25:" + e.message); }
}

async function saveRoomConfig(id) {
  try {
    const hw = ($(`#hw-${id}`).value || "").split("\n").map(s => s.trim()).filter(Boolean);
    const hk = ($(`#hk-${id}`).value || "").split("\n").map(s => s.trim()).filter(Boolean);
    const al = {}; ($(`#al-${id}`).value || "").split("\n").forEach(line => { const eq = line.indexOf("="); if (eq > 0) al[line.slice(0, eq).trim()] = line.slice(eq + 1).trim(); });
    const bt = ($(`#bt-${id}`).value || "").split("\n").map(s => s.trim()).filter(Boolean);
    const highlightMlMode = $(`#ml-${id}`).value || "inherit";
    await api("PATCH", `/api/rooms/${id}`, { room_config: { hotwords: hw, aliases: al, highlight_keywords: hk, blocked_topics: bt, highlight_ml_mode: highlightMlMode } });
    toast("\u623f\u95f4\u914d\u7f6e\u5df2\u4fdd\u5b58");
  } catch (e) { toast("\u4fdd\u5b58\u5931\u8d25:" + e.message); }
}

// ----------------------------- V0.1.2 \u9608\u503c\u81ea\u5b66\u4e60\u6458\u8981 ----------------------------- //
async function loadThresholdLearning(roomId) {
  try {
    const tl = await api("GET", `/api/rooms/${roomId}/threshold-learning`);
    const div = $(`#tl-${roomId}`);
    if (!div) return;
    if (!tl.samples) {
      div.innerHTML = `<span class="muted">\u9608\u503c\u81ea\u5b66\u4e60:\u5c1a\u65e0\u53cd\u9988\u6837\u672c</span>`;
      return;
    }
    const approvedRange = tl.approved_range
      ? `${tl.approved_range[0]}\u2013${tl.approved_range[1]}`
      : "\u2014";
    const rejectedRange = tl.rejected_range
      ? `${tl.rejected_range[0]}\u2013${tl.rejected_range[1]}`
      : "\u2014";
    let recHtml = "";
    if (tl.recommended && tl.recommended !== tl.current_threshold) {
      recHtml = ` \u00b7 <span style="color:var(--accent)">\u63a8\u8350\u9608\u503c:${tl.recommended}</span>`;
    }
    div.innerHTML = `
      <div class="row">
        <span class="score-chip">\u6837\u672c:${tl.samples}</span>
        <span class="score-chip">\u5f53\u524d\u9608\u503c:${tl.current_threshold}</span>
        <span class="score-chip">\u901a\u8fc7\u5206:${approvedRange}</span>
        <span class="score-chip">\u62d2\u7edd\u5206:${rejectedRange}</span>
        ${recHtml}
        ${!tl.ready ? `<span class="muted">(\u9700${tl.min_samples}\u6761)</span>` : ""}
      </div>`;
  } catch (e) { console.warn("\u52a0\u8f7d\u5931\u8d25:", e); }
}

// ----------------------------- V0.1.2 \u6e32\u67d3:\u5f55\u5236\u9884\u7ea6 ----------------------------- //
async function loadSchedules() {
  const data = await api("GET", "/api/dashboard");
  let roomOpts = data.rooms.map((r) =>
    `<option value="${r.id}">#${r.id} ${esc(r.title || r.input_url)}</option>`
  ).join("");
  $("#schedule-room").innerHTML = roomOpts;

  const rows = await api("GET", "/api/schedules");
  $("#schedules-list").innerHTML = rows.length ? rows.map((s) => `
    <div class="item">
      <div class="head">
        <div>
          <div class="title">\u9884\u7ea6 #${s.id} \u00b7 \u623f\u95f4 #${s.room_id} ${esc(s.uploader_name || s.room_title || "")}</div>
          <div class="sub">${esc(s.scheduled_at || "")} \u00b7 ${s.recurrent === "daily" ? "\u6bcf\u65e5" : "\u5355\u6b21"} \u00b7 ${s.triggered ? "\u5df2\u89e6\u53d1" : (s.enabled ? "\u7b49\u5f85\u4e2d" : "\u5df2\u7981\u7528")}</div>
        </div>
        <div class="actions">
          <button class="danger" onclick="delSchedule(${s.id})">\u5220\u9664</button>
        </div>
      </div>
    </div>`).join("") : `<div class="empty">\u6682\u65e0\u5f55\u5236\u9884\u7ea6\u3002</div>`;
}

async function delSchedule(id) {
  try { await api("DELETE", `/api/schedules/${id}`); toast("\u5df2\u5220\u9664"); loadSchedules(); }
  catch (e) { toast(e.message); }
}

// ----------------------------- V0.1.6 P1 \u6e32\u67d3:\u4e3b\u9898\u7ba1\u7406 ----------------------------- //
async function loadTopics() {
  try {
    const dbData = await api("GET", "/api/dashboard");
    const sessions = dbData.sessions || [];
    const sessionsWithActive = sessions.filter(s => s.status === "stopped" || s.status === "recording");
    let selHtml = '<option value="">\u9009\u62e9\u5f55\u5236\u4f1a\u8bdd</option>';
    for (const s of sessionsWithActive) {
      selHtml += `<option value="${s.id}">\u4f1a\u8bdd #${s.id} (\u623f\u95f4 ${s.room_id}) - ${s.status}</option>`;
    }
    $("#topic-session-select").innerHTML = selHtml;

    const data = await api("GET", "/api/topics");
    const topics = data.topics || [];
    $("#topics-list").innerHTML = topics.length ? topics.map(t => `
      <div class="item">
        <div class="head">
          <div>
            <div class="title">${esc(t.title || "\u672a\u547d\u540d\u4e3b\u9898")}</div>
            <div class="sub">\u7f6e\u4fe1\u5ea6 ${(t.confidence*100).toFixed(0)}% \u00b7 ${t.event_count} \u4e2a\u4e8b\u4ef6 \u00b7 ${esc(t.status)}</div>
            ${t.summary ? `<div class="sub">${esc(t.summary.substring(0,100))}</div>` : ""}
          </div>
          <div class="actions">
            <a href="/collection/${t.id}" target="_blank" style="text-decoration:none;color:var(--accent);font-size:12px">\ud83c\udfac \u7f16\u8f91\u5408\u96c6</a>
            <button onclick="toggleCollection(${t.id},${!t.is_collection})">
              ${t.is_collection ? "\u53d6\u6d88\u5408\u96c6" : "\u6807\u9002\u5408\u5408\u96c6"}
            </button>
          </div>
        </div>
      </div>`).join("") : `<div class="empty">\u6682\u65e0\u4e3b\u9898\u3002\u5148\u6267\u884c\u805a\u7c7b\u3002</div>`;
  } catch (e) { console.warn("\u52a0\u8f7d\u5931\u8d25:", e); }
}

async function toggleCollection(topicId, value) {
  try { await api("PATCH", `/api/topics/${topicId}`, {is_collection: value}); toast(value ? "\u5df2\u6807\u8bb0\u4e3a\u5408\u96c6" : "\u5df2\u53d6\u6d88\u5408\u96c6"); loadTopics(); }
  catch (e) { toast("\u64cd\u4f5c\u5931\u8d25:" + e.message); }
}

// ----------------------------- \u4e8b\u4ef6\u7ed1\u5b9a ----------------------------- //
$("#btn-add").addEventListener("click", async () => {
  const url = $("#new-url").value.trim();
  const authorized = $("#new-auth").checked;
  if (!url) return toast("\u8bf7\u8f93\u5165\u76f4\u64ad\u95f4 URL \u6216\u623f\u95f4\u53f7");
  try {
    await api("POST", "/api/rooms", { url, authorized });
    $("#new-url").value = "";
    toast("\u5df2\u6dfb\u52a0\u76f4\u64ad\u95f4");
    loadRooms();
  } catch (e) { toast("\u6dfb\u52a0\u5931\u8d25:" + e.message); }
});

$("#btn-add-schedule").addEventListener("click", async () => {
  const roomId = parseInt($("#schedule-room").value, 10);
  const time = $("#schedule-time").value;
  const daily = $("#schedule-daily").checked;
  if (!time) return toast("\u8bf7\u9009\u62e9\u9884\u7ea6\u65f6\u95f4");
  try {
    await api("POST", "/api/schedules", {
      room_id: roomId,
      scheduled_at: new Date(time).toISOString(),
      recurrent: daily ? "daily" : "",
    });
    toast("\u5df2\u521b\u5efa\u9884\u7ea6");
    loadSchedules();
  } catch (e) { toast("\u521b\u5efa\u5931\u8d25:" + e.message); }
});

$("#btn-cluster").addEventListener("click", async () => {
  const sid = $("#topic-session-select").value;
  if (!sid) { toast("\u8bf7\u5148\u9009\u62e9\u4e00\u4e2a\u5f55\u5236\u4f1a\u8bdd"); return; }
  toast("\u805a\u7c7b\u4e2d\u2026");
  try {
    const res = await api("POST", `/api/sessions/${sid}/cluster`);
    toast(`\u805a\u7c7b\u5b8c\u6210:${res.topics.length} \u4e2a\u4e3b\u9898`);
    loadTopics();
  } catch (e) { toast("\u805a\u7c7b\u5931\u8d25:" + e.message); }
});

export { loadRooms, saveRoom, saveRoomConfig, loadThresholdLearning, loadSchedules, delSchedule, loadTopics, toggleCollection };
